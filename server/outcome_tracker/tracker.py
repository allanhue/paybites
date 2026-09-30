"""
Outcome Tracker (v2)

Watches market.signals (real fired signals), market.scores (near-misses and
gate-blocked signals) and market.ticks (price), and resolves each tracked
entry into win / loss / timeout.

What changed vs v1
  1. Cooldown per (kind, symbol). v1 opened a new tracked "trade" on EVERY
     score tick inside the near-miss band (~10/sec), so 105k rows were mostly
     near-identical copies of a few hundred real situations. That inflated the
     database and made the model's validation look far better-powered than it
     is. NEAR_MISS_COOLDOWN_SECONDS / GATED_COOLDOWN_SECONDS fix that.
  2. Pending trades are indexed by symbol, so each price tick only touches that
     symbol's open entries instead of scanning every open entry.
  3. Optional volatility-scaled barriers (BARRIER_MODE=vol). Default stays
     'fixed' so labels remain comparable with your existing data.
  4. kind='gated': signals that crossed the threshold but were blocked by the
     analyst's regime gate / circuit breaker are tracked in the shadows, so you
     can compare gated vs. fired outcomes and see if the gates add value.
  5. Rolling stats (last ROLLING_N resolved entries) are written to Redis
     "monitor:rolling" - the analyst's circuit breaker and the dashboard read it.
  6. net_pct_change = pct_change - round-trip fee, stored per row.
"""

import json
import math
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

import psycopg
import redis
from dotenv import load_dotenv

load_dotenv()

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
DATABASE_URL = os.getenv("DATABASE_URL", "")
HOLD_MINUTES = float(os.getenv("OUTCOME_HOLD_MINUTES", "15"))
PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "0.3")) / 100
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.3")) / 100
NEAR_MISS_MARGIN = float(os.getenv("NEAR_MISS_MARGIN", "15.0"))
FLUSH_SECONDS = float(os.getenv("DB_FLUSH_SECONDS", "5"))

NEAR_MISS_COOLDOWN_SECONDS = float(os.getenv("NEAR_MISS_COOLDOWN_SECONDS", "120"))
GATED_COOLDOWN_SECONDS = float(os.getenv("GATED_COOLDOWN_SECONDS", "300"))

BARRIER_MODE = os.getenv("BARRIER_MODE", "fixed").lower()  # fixed | vol
BARRIER_VOL_K = float(os.getenv("BARRIER_VOL_K", "1.0"))
BARRIER_MIN = float(os.getenv("BARRIER_MIN_PCT", "0.15")) / 100
BARRIER_MAX = float(os.getenv("BARRIER_MAX_PCT", "1.0")) / 100

ROUND_TRIP_FEE = float(os.getenv("ROUND_TRIP_FEE_PCT", "0.20")) / 100
ROLLING_N = int(os.getenv("ROLLING_N", "200"))

host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), db=0)

DDL = """
CREATE TABLE IF NOT EXISTS trade_outcomes (
    id BIGSERIAL PRIMARY KEY,
    client_id UUID UNIQUE,
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    entry_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    confidence DOUBLE PRECISION NOT NULL,
    rsi_14 DOUBLE PRECISION,
    momentum DOUBLE PRECISION,
    band_position DOUBLE PRECISION,
    volatility DOUBLE PRECISION,
    macd_hist DOUBLE PRECISION,
    trend_bias DOUBLE PRECISION,
    exit_price DOUBLE PRECISION,
    outcome TEXT,
    pct_change DOUBLE PRECISION,
    resolved_at TIMESTAMPTZ
);
"""

NEW_COLUMNS = [
    ("client_id", "UUID"), ("rsi_14", "DOUBLE PRECISION"), ("momentum", "DOUBLE PRECISION"),
    ("band_position", "DOUBLE PRECISION"), ("volatility", "DOUBLE PRECISION"),
    ("macd_hist", "DOUBLE PRECISION"), ("trend_bias", "DOUBLE PRECISION"),
    ("gate", "TEXT"), ("barrier_pct", "DOUBLE PRECISION"), ("net_pct_change", "DOUBLE PRECISION"),
]

_insert_queue: list[dict] = []
_update_queue: list[dict] = []
_lock = threading.Lock()


def ensure_schema():
    if not DATABASE_URL:
        print("[tracker] DATABASE_URL not set - outcomes disabled")
        return
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            for name, typ in NEW_COLUMNS:
                cur.execute(f"ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS {name} {typ};")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS trade_outcomes_client_id_idx "
                "ON trade_outcomes(client_id);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS trade_outcomes_entry_time_idx "
                "ON trade_outcomes(entry_time);"
            )
        conn.commit()
    print("[tracker] schema ready")


def queue_insert(row: dict):
    with _lock:
        _insert_queue.append(row)


def queue_update(row: dict):
    with _lock:
        _update_queue.append(row)


def _flush():
    if not DATABASE_URL:
        return
    with _lock:
        inserts, _insert_queue[:] = list(_insert_queue), []
        updates, _update_queue[:] = list(_update_queue), []

    if not inserts and not updates:
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                if inserts:
                    cur.executemany(
                        """
                        INSERT INTO trade_outcomes
                            (client_id, kind, symbol, entry_price, confidence,
                             rsi_14, momentum, band_position, volatility, macd_hist, trend_bias,
                             gate, barrier_pct)
                        VALUES (%(client_id)s, %(kind)s, %(symbol)s, %(entry_price)s, %(confidence)s,
                                %(rsi_14)s, %(momentum)s, %(band_position)s, %(volatility)s,
                                %(macd_hist)s, %(trend_bias)s, %(gate)s, %(barrier_pct)s)
                        ON CONFLICT (client_id) DO NOTHING
                        """,
                        inserts,
                    )
                if updates:
                    cur.executemany(
                        """
                        UPDATE trade_outcomes
                        SET exit_price=%(exit_price)s, outcome=%(outcome)s,
                            pct_change=%(pct_change)s, net_pct_change=%(net_pct_change)s,
                            resolved_at=now()
                        WHERE client_id=%(client_id)s
                        """,
                        updates,
                    )
            conn.commit()
        print(f"[tracker] flushed {len(inserts)} inserts, {len(updates)} updates")
    except Exception as e:  # noqa: BLE001
        print(f"[tracker] flush failed, re-queueing: {e}")
        with _lock:
            _insert_queue[:0] = inserts
            _update_queue[:0] = updates


@dataclass
class PendingCheck:
    client_id: str
    kind: str
    symbol: str
    entry_price: float
    confidence: float
    started_at: float
    deadline: float
    target: float
    stop: float


# symbol -> open entries. RLock because finalize() -> write_monitor() re-enters.
pending: dict[str, list[PendingCheck]] = {}
_pending_lock = threading.RLock()
_last_track: dict[tuple[str, str], float] = {}

# (outcome, pct_change, net_pct) for the last ROLLING_N resolved signal/near_miss entries
_recent: deque = deque(maxlen=ROLLING_N)
_last_monitor = 0.0


def write_monitor(force: bool = False):
    global _last_monitor
    now = time.time()
    if not force and now - _last_monitor < 2:
        return
    _last_monitor = now
    with _pending_lock:
        snap = list(_recent)
        open_count = sum(len(v) for v in pending.values())
    wins = sum(1 for o, _, _ in snap if o == "win")
    losses = sum(1 for o, _, _ in snap if o == "loss")
    timeouts = sum(1 for o, _, _ in snap if o == "timeout")
    decided = wins + losses
    win_rate = 100.0 * wins / decided if decided else 0.0
    expectancy = 100.0 * sum(n for _, _, n in snap) / len(snap) if snap else 0.0
    try:
        r.hset("monitor:rolling", mapping={
            "n": decided, "wins": wins, "losses": losses, "timeouts": timeouts,
            "win_rate": round(win_rate, 2),
            "expectancy_net_pct": round(expectancy, 4),
            "open_entries": open_count,
            "window": ROLLING_N,
            "updated_at": now,
        })
    except redis.RedisError:
        pass


def start_background_flush():
    def loop():
        while True:
            time.sleep(FLUSH_SECONDS)
            _flush()
            write_monitor(force=True)
    threading.Thread(target=loop, daemon=True).start()


def start_retention_cleanup():
    def loop():
        while True:
            time.sleep(3600)
            if not DATABASE_URL:
                continue
            try:
                with psycopg.connect(DATABASE_URL) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM trade_outcomes WHERE kind IN ('near_miss','gated') "
                            "AND resolved_at IS NOT NULL AND resolved_at < now() - interval '14 days'"
                        )
                        deleted = cur.rowcount
                    conn.commit()
                if deleted:
                    print(f"[tracker] retention cleanup: removed {deleted} old rows")
            except Exception as e:  # noqa: BLE001
                print(f"[tracker] retention cleanup failed: {e}")
    threading.Thread(target=loop, daemon=True).start()


def barriers(volatility) -> tuple[float, float]:
    """(profit target, stop) as fractions. 'vol' mode scales to ~K sigma of the hold horizon."""
    if BARRIER_MODE == "vol":
        try:
            vol = float(volatility or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        b = BARRIER_VOL_K * vol * math.sqrt(HOLD_MINUTES)  # vol is per 1-min candle
        b = max(BARRIER_MIN, min(BARRIER_MAX, b))
        return b, b
    return PROFIT_TARGET_PCT, STOP_LOSS_PCT


def handle_signal(payload: dict, kind: str, gate: str | None = None):
    symbol = payload["symbol"]
    price = float(payload.get("trigger_price") or payload.get("price"))
    confidence = float(payload.get("confidence") or payload.get("score"))
    client_id = str(uuid.uuid4())
    target, stop = barriers(payload.get("volatility"))

    queue_insert({
        "client_id": client_id, "kind": kind, "symbol": symbol,
        "entry_price": price, "confidence": confidence,
        "rsi_14": payload.get("rsi_14"), "momentum": payload.get("momentum"),
        "band_position": payload.get("band_position"), "volatility": payload.get("volatility"),
        "macd_hist": payload.get("macd_hist"), "trend_bias": payload.get("trend_bias"),
        "gate": gate, "barrier_pct": target,
    })

    now = time.time()
    with _pending_lock:
        pending.setdefault(symbol, []).append(PendingCheck(
            client_id=client_id, kind=kind, symbol=symbol, entry_price=price,
            confidence=confidence, started_at=now, deadline=now + HOLD_MINUTES * 60,
            target=target, stop=stop,
        ))

    channel = "trade.outcomes" if kind == "signal" else "trade.missed"
    r.publish(channel, json.dumps({"symbol": symbol, "price": price, "confidence": confidence, "status": "pending"}))


def maybe_track(payload: dict, kind: str, cooldown: float, gate: str | None = None):
    key = (kind, payload["symbol"])
    now = time.time()
    if now - _last_track.get(key, 0.0) < cooldown:
        return
    _last_track[key] = now
    handle_signal(payload, kind=kind, gate=gate)


def check_pending_against_price(symbol: str, price: float):
    with _pending_lock:
        entries = pending.get(symbol)
        if not entries:
            return
        now = time.time()
        keep = []
        for p in entries:
            change = (price - p.entry_price) / p.entry_price
            if change >= p.target:
                finalize(p, price, "win", change)
            elif change <= -p.stop:
                finalize(p, price, "loss", change)
            elif now >= p.deadline:
                finalize(p, price, "timeout", change)
            else:
                keep.append(p)
        pending[symbol] = keep


def finalize(p: PendingCheck, exit_price: float, outcome: str, pct_change: float):
    net = pct_change - ROUND_TRIP_FEE
    queue_update({
        "client_id": p.client_id, "exit_price": exit_price,
        "outcome": outcome, "pct_change": pct_change, "net_pct_change": net,
    })
    if p.kind in ("signal", "near_miss"):  # gated entries are shadow-only, kept out of the health stats
        _recent.append((outcome, pct_change, net))
        write_monitor()
    channel = "trade.outcomes" if p.kind == "signal" else "trade.missed"
    r.publish(channel, json.dumps({
        "symbol": p.symbol, "entry_price": p.entry_price, "exit_price": exit_price,
        "confidence": p.confidence, "outcome": outcome, "kind": p.kind,
        "pct_change": round(pct_change * 100, 3), "status": "resolved",
    }))
    print(f"[tracker] {p.kind} {p.symbol} -> {outcome} ({pct_change*100:.2f}%)")


def main():
    ensure_schema()
    start_background_flush()
    start_retention_cleanup()

    pubsub = r.pubsub()
    pubsub.subscribe("market.signals", "market.scores", "market.ticks")
    print(
        f"Outcome tracker v2 online. barriers={BARRIER_MODE} "
        f"near_miss_cooldown={NEAR_MISS_COOLDOWN_SECONDS}s fee={ROUND_TRIP_FEE*100:.2f}%"
    )

    for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            payload = json.loads(message["data"].decode("utf-8"))
        except json.JSONDecodeError:
            continue

        channel = message["channel"].decode()

        if channel == "market.signals":
            handle_signal(payload, kind="signal")
        elif channel == "market.scores":
            threshold = float(payload.get("threshold", 75.0))
            score = float(payload["score"])
            gate = payload.get("blocked_by")
            entry = {
                "symbol": payload["symbol"], "price": payload["price"],
                "confidence": score, "rsi_14": payload.get("rsi_14"),
                "momentum": payload.get("momentum"), "band_position": payload.get("band_position"),
                "volatility": payload.get("volatility"), "macd_hist": payload.get("macd_hist"),
                "trend_bias": payload.get("trend_bias"),
            }
            if score > threshold and gate in ("regime", "breaker"):
                maybe_track(entry, "gated", GATED_COOLDOWN_SECONDS, gate=gate)
            elif threshold - NEAR_MISS_MARGIN <= score <= threshold:
                maybe_track(entry, "near_miss", NEAR_MISS_COOLDOWN_SECONDS)
        elif channel == "market.ticks":
            check_pending_against_price(payload["symbol"], float(payload["price"]))


if __name__ == "__main__":
    main()