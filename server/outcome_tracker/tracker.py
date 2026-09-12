"""
Outcome Tracker

Watches two things:
  1. Every fired BUY signal ("market.signals") — tracks whether price actually
     moved up by PROFIT_TARGET_PCT before down by STOP_LOSS_PCT within
     HOLD_MINUTES. Labels it win/loss/timeout. This is your real backtest data.
  2. Every "near miss" scored tick ("market.scores" with score just under
     threshold) — same check, to answer "would this have been a good trade
     if the threshold were slightly lower?" Feeds the Missed Opportunities panel.

Writes results to Neon (batched) and publishes to Redis for live dashboard
notifications.
"""

import json
import os
import threading
import time
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
NEAR_MISS_MARGIN = float(os.getenv("NEAR_MISS_MARGIN", "15.0"))  # points below threshold

host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), db=0)

DDL = """
CREATE TABLE IF NOT EXISTS trade_outcomes (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,              -- 'signal' or 'near_miss'
    symbol TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    entry_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    confidence DOUBLE PRECISION NOT NULL,
    exit_price DOUBLE PRECISION,
    outcome TEXT,                    -- 'win' | 'loss' | 'timeout'
    pct_change DOUBLE PRECISION,
    resolved_at TIMESTAMPTZ
);
"""


def ensure_schema():
    if not DATABASE_URL:
        print("[tracker] DATABASE_URL not set — outcomes disabled")
        return
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    print("[tracker] schema ready")


@dataclass
class PendingCheck:
    kind: str
    symbol: str
    entry_price: float
    confidence: float
    started_at: float
    deadline: float
    row_id: int = field(default=-1)


pending: list[PendingCheck] = []
_lock = threading.Lock()


def insert_pending(kind: str, symbol: str, price: float, confidence: float) -> int:
    if not DATABASE_URL:
        return -1
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trade_outcomes (kind, symbol, entry_price, confidence) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (kind, symbol, price, confidence),
            )
            row_id = cur.fetchone()[0]
        conn.commit()
    return row_id


def resolve(row_id: int, exit_price: float, outcome: str, pct_change: float):
    if not DATABASE_URL or row_id < 0:
        return
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trade_outcomes SET exit_price=%s, outcome=%s, "
                "pct_change=%s, resolved_at=now() WHERE id=%s",
                (exit_price, outcome, pct_change, row_id),
            )
        conn.commit()


def handle_signal(payload: dict, kind: str):
    symbol = payload["symbol"]
    price = float(payload.get("trigger_price") or payload.get("price"))
    confidence = float(payload.get("confidence") or payload.get("score"))

    row_id = insert_pending(kind, symbol, price, confidence)
    now = time.time()
    with _lock:
        pending.append(PendingCheck(
            kind=kind, symbol=symbol, entry_price=price, confidence=confidence,
            started_at=now, deadline=now + HOLD_MINUTES * 60, row_id=row_id,
        ))
    channel = "trade.outcomes" if kind == "signal" else "trade.missed"
    r.publish(channel, json.dumps({
        "symbol": symbol, "price": price, "confidence": confidence, "status": "pending",
    }))


def check_pending_against_price(symbol: str, price: float):
    with _lock:
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
    resolve(p.row_id, exit_price, outcome, pct_change)
    channel = "trade.outcomes" if p.kind == "signal" else "trade.missed"
    r.publish(channel, json.dumps({
        "symbol": p.symbol, "entry_price": p.entry_price, "exit_price": exit_price,
        "confidence": p.confidence, "outcome": outcome,
        "pct_change": round(pct_change * 100, 3), "status": "resolved",
    }))
    print(f"[tracker] {p.kind} {p.symbol} -> {outcome} ({pct_change*100:.2f}%)")


def main():
    ensure_schema()

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
                    "confidence": score,
                }, kind="near_miss")

        elif channel == "market.ticks":
            check_pending_against_price(payload["symbol"], float(payload["price"]))


if __name__ == "__main__":
    main()