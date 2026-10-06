"""
Execution Bridge v3

go-shield (automated mode) or the dashboard's Approve button publishes a trade to "trade.execute".
This service is the only place that calls a broker with real money.
Routing: *USDT/*BUSD/*USDC -> Binance spot, everything else -> MT5 (Exness).

v3 - what changed and why
  v1/v2 only ever BOUGHT. Nothing sold, so every approved trade stayed open with no stop-loss.
  Now every filled entry gets an exit plan and a position record (kept in Redis, so a restart
  does not forget your open trades):
    - stop-loss  : EXIT_STOP_PCT   (default 0.3)
    - take-profit: EXIT_TP_PCT     (default 0.3)
    - time exit  : EXIT_MAX_HOLD_MINUTES (default 15)
  Defaults match the outcome tracker's 0.3% / 0.3% / 15-minute rules so live results are directly
  comparable with the tracked ones. Keep them in sync if you change either side.
  Binance exits are managed by THIS process (polling the exchange price every EXIT_POLL_SECONDS):
  if the bridge or your internet dies, an open Binance position is NOT protected. MT5 orders carry
  a server-side stop-loss and take-profit, so those survive a crash; only the time exit needs us.

Safety limits (all enforced before any new entry)
  - Kill switch  : redis key  config:kill_switch = 1   (existing positions are still closed)
  - Daily loss   : DAILY_LOSS_LIMIT_BINANCE_USD (default 1.0), DAILY_LOSS_LIMIT_MT5 (account
                   currency, default 100 = $1 on a cent account)
  - Max open     : MAX_OPEN_POSITIONS (default 3), one position per symbol
  - Size         : TRADE_USD_AMOUNT (Binance), TRADE_LOT_SIZE (MT5)
  - BINANCE_TESTNET defaults to true. Use separate testnet API keys.

Realised P&L is an estimate on Binance (fees taken from BINANCE_FEE_RATE_PCT, default 0.10 per side).
Check the broker's own order history before trusting any number.
"""

import json
import math
import os
import threading
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5
import redis
from binance.client import Client
from dotenv import load_dotenv

load_dotenv()

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), db=0)

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
TRADE_USD_AMOUNT = float(os.getenv("TRADE_USD_AMOUNT", "5.00"))
FEE_RATE = float(os.getenv("BINANCE_FEE_RATE_PCT", "0.10")) / 100

MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
TRADE_LOT_SIZE = float(os.getenv("TRADE_LOT_SIZE", "0.01"))
MAGIC = 998877

STOP_PCT = float(os.getenv("EXIT_STOP_PCT", "0.3")) / 100
TP_PCT = float(os.getenv("EXIT_TP_PCT", "0.3")) / 100
MAX_HOLD_MIN = float(os.getenv("EXIT_MAX_HOLD_MINUTES", "15"))
POLL_SECONDS = float(os.getenv("EXIT_POLL_SECONDS", "3"))
MAX_OPEN = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
DAILY_LOSS_LIMIT = {
    "binance": float(os.getenv("DAILY_LOSS_LIMIT_BINANCE_USD", "1.0")),
    "mt5": float(os.getenv("DAILY_LOSS_LIMIT_MT5", "100")),
}
MAX_EXIT_FAILS = 5

binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=BINANCE_TESTNET)
mt5_ready = mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
if not mt5_ready:
    print(f"[bridge] MT5 not initialized: {mt5.last_error()} - forex orders will fail until fixed")

POS_KEY = "exec:positions"
positions: dict[str, dict] = {}
_lock = threading.RLock()


# --------------------------------------------------------------------------- helpers
def is_crypto(symbol: str) -> bool:
    return symbol.upper().endswith(("USDT", "BUSD", "USDC"))


def broker_of(symbol: str) -> str:
    return "binance" if is_crypto(symbol) else "mt5"


def round_step_size(quantity: float, step_size: float) -> float:
    precision = max(0, int(round(-math.log10(step_size))))
    return math.floor(quantity * (10 ** precision) + 1e-9) / (10 ** precision)  # epsilon: 0.06+0.01 must be 0.07


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def day_pnl(broker: str) -> float:
    return float(r.get(f"exec:pnl:{broker}:{_day()}") or 0.0)


def add_pnl(broker: str, value: float) -> None:
    key = f"exec:pnl:{broker}:{_day()}"
    r.incrbyfloat(key, value)
    r.expire(key, 3 * 86400)


def publish(result: dict) -> None:
    r.publish("trade.executed", json.dumps(result))


def load_positions() -> None:
    for _, raw in r.hgetall(POS_KEY).items():
        p = json.loads(raw)
        positions[p["id"]] = p
    if positions:
        print(f"[bridge] recovered {len(positions)} open position(s) from Redis: {list(positions)}")


def save_position(p: dict) -> None:
    with _lock:
        positions[p["id"]] = p
        r.hset(POS_KEY, p["id"], json.dumps(p))


def drop_position(pid: str) -> None:
    with _lock:
        positions.pop(pid, None)
        r.hdel(POS_KEY, pid)


def can_open(symbol: str, broker: str):
    """Return a reason string if a new entry must be refused, else None."""
    if (r.get("config:kill_switch") or b"").decode().lower() in ("1", "true", "on"):
        return "kill switch is on"
    pnl = day_pnl(broker)
    if pnl <= -DAILY_LOSS_LIMIT[broker]:
        return f"daily loss limit reached ({broker} {pnl:+.2f} <= -{DAILY_LOSS_LIMIT[broker]})"
    with _lock:
        if len(positions) >= MAX_OPEN:
            return f"max open positions reached ({MAX_OPEN})"
        if any(p["symbol"] == symbol for p in positions.values()):
            return f"position already open in {symbol}"
    return None


# ----------------------------------------------------------------------------- Binance
def symbol_rules(symbol: str):
    info = binance_client.get_symbol_info(symbol)
    step, min_qty, min_notional = 0.00001, 0.0, 0.0
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step, min_qty = float(f["stepSize"]), float(f["minQty"])
        elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
            min_notional = float(f.get("minNotional") or 0)
    return info["baseAsset"], step, min_qty, min_notional


def avg_fill(order: dict) -> float:
    fills = order.get("fills") or []
    qty = sum(float(f["qty"]) for f in fills)
    if not qty:
        return 0.0
    return sum(float(f["price"]) * float(f["qty"]) for f in fills) / qty


def size_entry(price: float, step: float, min_qty: float, min_notional: float):
    """Smallest quantity >= TRADE_USD_AMOUNT that can still be SOLD after the buy fee (taken in the
    coin, rounded down to a step) and a full stop-loss move. Returns None if that would cost >1.5x
    the configured trade size, because a position that cannot be sold is worse than no position."""
    qty = round_step_size(TRADE_USD_AMOUNT / price, step)
    for _ in range(25):
        net = round_step_size(qty * 0.998, step)  # worst case fee paid in the base coin
        sellable_usd = net * price * (1 - STOP_PCT - 0.005)
        if qty >= min_qty and net >= min_qty and qty * price >= min_notional and sellable_usd >= min_notional:
            break
        qty = round_step_size(qty + step, step)
    else:
        return None
    return qty if qty * price <= TRADE_USD_AMOUNT * 1.5 else None


def open_binance(symbol: str):
    base, step, min_qty, min_notional = symbol_rules(symbol)
    price = float(binance_client.get_symbol_ticker(symbol=symbol)["price"])
    qty = size_entry(price, step, min_qty, min_notional)
    if qty is None:
        return {"status": "failed", "reason": f"cannot size {symbol} safely near ${TRADE_USD_AMOUNT}: exchange minimum "
                f"order value is ${min_notional} and the position must stay sellable after fees and a stop-loss move. "
                "Raise TRADE_USD_AMOUNT (BTC needs roughly $10-15 at current prices)."}, None
    order = binance_client.create_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
    fills = order.get("fills") or []
    base_fee = sum(float(f["commission"]) for f in fills if f.get("commissionAsset") == base)
    net_qty = round_step_size(float(order.get("executedQty", qty)) - base_fee, step)
    entry = avg_fill(order) or price
    now = time.time()
    pos = {
        "id": f"{symbol}-{int(now)}", "broker": "binance", "symbol": symbol, "qty": net_qty,
        "entry": entry, "opened_at": now, "deadline": now + MAX_HOLD_MIN * 60,
        "stop_price": entry * (1 - STOP_PCT), "tp_price": entry * (1 + TP_PCT), "fails": 0, "stuck": False,
    }
    return {"status": "filled", "broker": "binance", "order_id": order.get("orderId"), "qty": net_qty,
            "entry": entry, "stop": pos["stop_price"], "take_profit": pos["tp_price"]}, pos


def close_binance(p: dict, reason: str):
    base, step, min_qty, min_notional = symbol_rules(p["symbol"])
    free = float(binance_client.get_asset_balance(asset=base)["free"])
    qty = round_step_size(min(p["qty"], free), step)
    price = float(binance_client.get_symbol_ticker(symbol=p["symbol"])["price"])
    if qty <= 0 or qty < min_qty or qty * price < min_notional:
        return None, f"cannot sell {qty} {base}: below exchange minimum (dust). Close it manually in Binance."
    order = binance_client.create_order(symbol=p["symbol"], side="SELL", type="MARKET", quantity=qty)
    exit_p = avg_fill(order) or price
    pnl = (exit_p - p["entry"]) * qty - (p["entry"] * qty + exit_p * qty) * FEE_RATE
    return {"status": "closed", "broker": "binance", "symbol": p["symbol"], "reason": reason,
            "entry": p["entry"], "exit": exit_p, "qty": qty, "pnl": round(pnl, 4)}, None


# -------------------------------------------------------------------------------- MT5
def open_mt5(symbol: str):
    if not mt5_ready:
        return {"status": "failed", "reason": "MT5 not initialized"}, None
    info, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return {"status": "failed", "reason": f"no symbol/tick data for {symbol}"}, None
    ask, digits = tick.ask, info.digits
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": TRADE_LOT_SIZE,
        "type": mt5.ORDER_TYPE_BUY, "price": ask,
        "sl": round(ask * (1 - STOP_PCT), digits), "tp": round(ask * (1 + TP_PCT), digits),
        "deviation": 10, "magic": MAGIC, "comment": "paybites-bot3",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        why = "no response" if result is None else f"retcode {result.retcode}: {result.comment}"
        return {"status": "failed", "reason": why}, None
    mine = [x for x in (mt5.positions_get(symbol=symbol) or []) if x.magic == MAGIC]
    if not mine:
        return {"status": "failed", "reason": "order accepted but position not found - check MT5 manually"}, None
    pos_obj = max(mine, key=lambda x: x.time_msc)
    now = time.time()
    pos = {
        "id": f"{symbol}-{pos_obj.ticket}", "broker": "mt5", "symbol": symbol, "qty": TRADE_LOT_SIZE,
        "ticket": pos_obj.ticket, "entry": pos_obj.price_open, "opened_at": now,
        "deadline": now + MAX_HOLD_MIN * 60, "fails": 0, "stuck": False,
    }
    return {"status": "filled", "broker": "exness_mt5", "order_id": result.order, "lot": TRADE_LOT_SIZE,
            "entry": pos_obj.price_open, "stop": request["sl"], "take_profit": request["tp"]}, pos


def mt5_realised(ticket: int) -> float:
    deals = mt5.history_deals_get(position=ticket) or []
    return float(sum(d.profit + d.commission + d.swap for d in deals))


def close_mt5(p: dict, reason: str):
    tick = mt5.symbol_info_tick(p["symbol"])
    if tick is None:
        return None, f"no tick data for {p['symbol']}"
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": p["symbol"], "volume": p["qty"],
        "type": mt5.ORDER_TYPE_SELL, "position": p["ticket"], "price": tick.bid,
        "deviation": 20, "magic": MAGIC, "comment": "paybites-exit",
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        return None, "no response" if result is None else f"retcode {result.retcode}: {result.comment}"
    return {"status": "closed", "broker": "exness_mt5", "symbol": p["symbol"], "reason": reason,
            "entry": p["entry"], "exit": tick.bid, "qty": p["qty"], "pnl": round(mt5_realised(p["ticket"]), 4)}, None


# --------------------------------------------------------------------- position manager
def finish(p: dict, result: dict) -> None:
    drop_position(p["id"])
    add_pnl(p["broker"], result.get("pnl", 0.0))
    publish(result)
    print(f"[bridge] closed {p['symbol']} ({result['reason']}) pnl={result.get('pnl')}")


def exit_failed(p: dict, err: str) -> None:
    p["fails"] = p.get("fails", 0) + 1
    print(f"[bridge] exit failed for {p['symbol']} ({p['fails']}/{MAX_EXIT_FAILS}): {err}")
    if p["fails"] >= MAX_EXIT_FAILS and not p.get("stuck"):
        p["stuck"] = True
        publish({"status": "exit_failed", "symbol": p["symbol"], "reason": err, "position": p["id"]})
    save_position(p)


def check_position(p: dict) -> None:
    now = time.time()
    if p["broker"] == "binance":
        price = float(binance_client.get_symbol_ticker(symbol=p["symbol"])["price"])
        reason = ("stop_loss" if price <= p["stop_price"] else "take_profit" if price >= p["tp_price"]
                  else "time_exit" if now >= p["deadline"] else None)
        if not reason:
            return
        result, err = close_binance(p, reason)
    else:
        if not mt5.positions_get(ticket=p["ticket"]):  # server-side SL/TP already closed it
            finish(p, {"status": "closed", "broker": "exness_mt5", "symbol": p["symbol"], "reason": "closed_by_broker",
                       "entry": p["entry"], "pnl": round(mt5_realised(p["ticket"]), 4)})
            return
        if now < p["deadline"]:
            return
        result, err = close_mt5(p, "time_exit")
    if result:
        finish(p, result)
    else:
        exit_failed(p, err)


def monitor_loop() -> None:
    while True:
        time.sleep(POLL_SECONDS)
        with _lock:
            snapshot = [p for p in positions.values() if not p.get("stuck")]
        for p in snapshot:
            try:
                check_position(p)
            except Exception as e:  # noqa: BLE001 - one bad position must not stop the others
                exit_failed(p, str(e))


# ------------------------------------------------------------------------------- main
def handle_trade(trade: dict) -> None:
    symbol = trade["symbol"]
    broker = broker_of(symbol)
    refusal = can_open(symbol, broker)
    if refusal:
        print(f"[bridge] {symbol}: refused - {refusal}")
        publish({"status": "rejected", "symbol": symbol, "reason": refusal})
        return
    try:
        result, pos = open_binance(symbol) if broker == "binance" else open_mt5(symbol)
    except Exception as e:  # noqa: BLE001
        result, pos = {"status": "failed", "reason": str(e)}, None
    if pos:
        save_position(pos)
    result["symbol"] = symbol
    print(f"[bridge] {symbol}: {result}")
    publish(result)


def main() -> None:
    load_positions()
    threading.Thread(target=monitor_loop, daemon=True).start()
    pubsub = r.pubsub()
    pubsub.subscribe("trade.execute")
    print(f"Execution bridge v3 online. testnet={BINANCE_TESTNET} size=${TRADE_USD_AMOUNT} "
          f"stop={STOP_PCT*100:.2f}% tp={TP_PCT*100:.2f}% hold={MAX_HOLD_MIN}min max_open={MAX_OPEN} "
          f"daily_loss_limit={DAILY_LOSS_LIMIT}")
    if not BINANCE_TESTNET:
        print("[bridge] *** BINANCE_TESTNET=false: REAL MONEY ***")

    for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            trade = json.loads(message["data"].decode("utf-8"))
            handle_trade(trade)
        except Exception as e:  # noqa: BLE001
            print(f"[bridge] bad message ignored: {e}")


if __name__ == "__main__":
    main()