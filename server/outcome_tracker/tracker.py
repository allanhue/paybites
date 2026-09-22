"""
Outcome Tracker

Watches market.signals (real fired signals) and market.scores (near-misses),
tracks each until it resolves into win/loss/timeout, and writes to Aiven.
"""

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

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

_insert_queue: list[dict] = []
_update_queue: list[dict] = []
_lock = threading.Lock()


def ensure_schema():
    if not DATABASE_URL:
        print("[tracker] DATABASE_URL not set — outcomes disabled")
        return
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS client_id UUID;")
            cur.execute("ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS rsi_14 DOUBLE PRECISION;")
            cur.execute("ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS momentum DOUBLE PRECISION;")
            cur.execute("ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS band_position DOUBLE PRECISION;")
            cur.execute("ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS volatility DOUBLE PRECISION;")
            cur.execute("ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS macd_hist DOUBLE PRECISION;")
            cur.execute("ALTER TABLE trade_outcomes ADD COLUMN IF NOT EXISTS trend_bias DOUBLE PRECISION;")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS trade_outcomes_client_id_idx "
                "ON trade_outcomes(client_id);"
            )
        conn.commit()
    print("[tracker] schema ready (Aiven)")


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
                             rsi_14, momentum, band_position, volatility, macd_hist, trend_bias)
                        VALUES (%(client_id)s, %(kind)s, %(symbol)s, %(entry_price)s, %(confidence)s,
                                %(rsi_14)s, %(momentum)s, %(band_position)s, %(volatility)s,
                                %(macd_hist)s, %(trend_bias)s)
                        ON CONFLICT (client_id) DO NOTHING
                        """,
                        inserts,
                    )
                if updates:
                    cur.executemany(
                        """
                        UPDATE trade_outcomes
                        SET exit_price=%(exit_price)s, outcome=%(outcome)s,
                            pct_change=%(pct_change)s, resolved_at=now()
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


def start_background_flush():
    def loop():
        while True:
            time.sleep(FLUSH_SECONDS)
            _flush()
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
                            "DELETE FROM trade_outcomes WHERE kind = 'near_miss' "
                            "AND resolved_at IS NOT NULL AND resolved_at < now() - interval '14 days'"
                        )
                        deleted = cur.rowcount
                    conn.commit()
                if deleted:
                    print(f"[tracker] retention cleanup: removed {deleted} old near_miss rows")
            except Exception as e:
                print(f"[tracker] retention cleanup failed: {e}")
    threading.Thread(target=loop, daemon=True).start()


@dataclass
class PendingCheck:
    client_id: str
    kind: str
    symbol: str
    entry_price: float
    confidence: float
    started_at: float
    deadline: float


pending: list[PendingCheck] = []
_pending_lock = threading.Lock()


def handle_signal(payload: dict, kind: str):
    symbol = payload["symbol"]
    price = float(payload.get("trigger_price") or payload.get("price"))
    confidence = float(payload.get("confidence") or payload.get("score"))
    client_id = str(uuid.uuid4())

    queue_insert({
        "client_id": client_id, "kind": kind, "symbol": symbol,
        "entry_price": price, "confidence": confidence,
        "rsi_14": payload.get("rsi_14"), "momentum": payload.get("momentum"),
        "band_position": payload.get("band_position"), "volatility": payload.get("volatility"),
        "macd_hist": payload.get("macd_hist"), "trend_bias": payload.get("trend_bias"),
    })

    now = time.time()
    with _pending_lock:
        pending.append(PendingCheck(
            client_id=client_id, kind=kind, symbol=symbol, entry_price=price,
            confidence=confidence, started_at=now, deadline=now + HOLD_MINUTES * 60,
        ))

    channel = "trade.outcomes" if kind == "signal" else "trade.missed"
    r.publish(channel, json.dumps({"symbol": symbol, "price": price, "confidence": confidence, "status": "pending"}))


def check_pending_against_price(symbol: str, price: float):
    with _pending_lock:
        still_pending = []
        for p in pending:
            if p.symbol != symbol:
                still_pending.append(p)
                continue
            change = (price - p.entry_price) / p.entry_price
            now = time.time()
            if change >= PROFIT_TARGET_PCT:
                finalize(p, price, "win", change)
            elif change <= -STOP_LOSS_PCT:
                finalize(p, price, "loss", change)
            elif now >= p.deadline:
                finalize(p, price, "timeout", change)
            else:
                still_pending.append(p)
        pending[:] = still_pending


def finalize(p: PendingCheck, exit_price: float, outcome: str, pct_change: float):
    queue_update({
        "client_id": p.client_id, "exit_price": exit_price,
        "outcome": outcome, "pct_change": pct_change,
    })
    channel = "trade.outcomes" if p.kind == "signal" else "trade.missed"
    r.publish(channel, json.dumps({
        "symbol": p.symbol, "entry_price": p.entry_price, "exit_price": exit_price,
        "confidence": p.confidence, "outcome": outcome,
        "pct_change": round(pct_change * 100, 3), "status": "resolved",
    }))
    print(f"[tracker] {p.kind} {p.symbol} -> {outcome} ({pct_change*100:.2f}%)")


def main():
    ensure_schema()
    start_background_flush()
    start_retention_cleanup()

    pubsub = r.pubsub()
    pubsub.subscribe("market.signals", "market.scores", "market.ticks")
    print("Outcome tracker online.")

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
            if threshold - NEAR_MISS_MARGIN <= score < threshold:
                handle_signal({
                    "symbol": payload["symbol"], "price": payload["price"],
                    "confidence": score, "rsi_14": payload["rsi_14"],
                    "momentum": payload["momentum"], "band_position": payload["band_position"],
                    "volatility": payload["volatility"], "macd_hist": payload.get("macd_hist"),
                    "trend_bias": payload.get("trend_bias"),
                }, kind="near_miss")
        elif channel == "market.ticks":
            check_pending_against_price(payload["symbol"], float(payload["price"]))


if __name__ == "__main__":
    main()