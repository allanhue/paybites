"""
Execution Bridge

go-shield publishes trades that already passed risk checks to "trade.execute".
This service is the only place that actually calls a broker with real money.
It routes by symbol shape: *USDT/*BUSD -> Binance, 6-letter currency pairs -> MT5.

v2 fixes
  - Reads `trigger_price` (what go-shield / the Approve button actually send).
    Before, price defaulted to 0 and every Binance order died with ZeroDivisionError.
  - Falls back to the live Binance ticker if no usable price arrives.
  - Per-symbol lock so one signal burst cannot open several positions.

STILL MISSING (do not use automated mode until added): this bridge only BUYs.
Nothing sells, so there is no stop-loss / take-profit / time exit.
"""

import json
import math
import os

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

MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
TRADE_LOT_SIZE = float(os.getenv("TRADE_LOT_SIZE", "0.01"))

LOCK_SECONDS = int(os.getenv("POSITION_LOCK_SECONDS", "900"))

binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=BINANCE_TESTNET)
mt5_ready = mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
if not mt5_ready:
    print(f"[bridge] MT5 not initialized: {mt5.last_error()} — forex orders will fail until fixed")


def is_crypto(symbol: str) -> bool:
    return symbol.upper().endswith(("USDT", "BUSD", "USDC"))


def round_step_size(quantity: float, step_size: float) -> float:
    precision = int(round(-math.log10(step_size)))
    return math.floor(quantity * (10 ** precision)) / (10 ** precision)


def execute_binance(symbol: str, price: float) -> dict:
    info = binance_client.get_symbol_info(symbol)
    step_size = 0.00001
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
            break

    if price <= 0:
        price = float(binance_client.get_symbol_ticker(symbol=symbol)["price"])

    raw_qty = TRADE_USD_AMOUNT / price
    qty = round_step_size(raw_qty, step_size)
    if qty <= 0:
        return {"status": "failed", "reason": "computed quantity rounds to zero"}

    order = binance_client.create_order(
        symbol=symbol, side="BUY", type="MARKET", quantity=qty
    )
    return {"status": "filled", "broker": "binance", "order_id": order.get("orderId"), "qty": qty}


def execute_mt5(symbol: str) -> dict:
    if not mt5_ready:
        return {"status": "failed", "reason": "MT5 not initialized"}
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"status": "failed", "reason": f"no tick data for {symbol}"}

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": TRADE_LOT_SIZE,
        "type": mt5.ORDER_TYPE_BUY,
        "price": tick.ask,
        "deviation": 10,
        "magic": 998877,
        "comment": "paybites-bot3",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"status": "failed", "reason": f"retcode {result.retcode}: {result.comment}"}
    return {"status": "filled", "broker": "exness_mt5", "order_id": result.order, "lot": TRADE_LOT_SIZE}


def main():
    pubsub = r.pubsub()
    pubsub.subscribe("trade.execute")
    print("Execution bridge online. Awaiting approved trades...")

    for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            trade = json.loads(message["data"].decode("utf-8"))
        except json.JSONDecodeError:
            continue

        symbol = trade["symbol"]
        price = float(trade.get("trigger_price") or trade.get("price") or 0)

        lock_key = f"exec:lock:{symbol}"
        if not r.set(lock_key, "1", nx=True, ex=LOCK_SECONDS):
            print(f"[bridge] {symbol}: skipped, position lock active")
            continue

        try:
            if is_crypto(symbol):
                result = execute_binance(symbol, price)
            else:
                result = execute_mt5(symbol)
        except Exception as e:
            result = {"status": "failed", "reason": str(e)}

        if result.get("status") != "filled":
            r.delete(lock_key)  # a failed order should not block a retry

        result["symbol"] = symbol
        print(f"[bridge] {symbol}: {result}")
        r.publish("trade.executed", json.dumps(result))


if __name__ == "__main__":
    main()