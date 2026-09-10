"""
Batch sync to Neon (Postgres).

Matches the "avoid database bottlenecks" guardrail: we never write per-tick
to Neon. Instead we buffer scored signals in memory and flush the whole
batch every DB_FLUSH_SECONDS on a background thread. If Neon is briefly
unreachable, the buffer just keeps growing until the next successful flush.
"""

import os
import threading
import time
from typing import Any

import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "")
FLUSH_SECONDS = float(os.getenv("DB_FLUSH_SECONDS", "15"))

_buffer: list[dict[str, Any]] = []
_lock = threading.Lock()

DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    rsi_14 DOUBLE PRECISION NOT NULL,
    volatility DOUBLE PRECISION NOT NULL,
    momentum DOUBLE PRECISION NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    action TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def ensure_schema() -> None:
    if not DATABASE_URL:
        print("[db] DATABASE_URL not set — Neon sync disabled, logging locally only")
        return
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    print("[db] Neon schema ready")


def queue_signal(record: dict[str, Any]) -> None:
    with _lock:
        _buffer.append(record)


def _flush() -> None:
    global _buffer
    if not DATABASE_URL:
        return
    with _lock:
        if not _buffer:
            return
        batch, _buffer = _buffer, []

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO signals
                        (symbol, price, rsi_14, volatility, momentum, confidence, action)
                    VALUES (%(symbol)s, %(price)s, %(rsi_14)s, %(volatility)s,
                            %(momentum)s, %(confidence)s, %(action)s)
                    """,
                    batch,
                )
            conn.commit()
        print(f"[db] flushed {len(batch)} signals to Neon")
    except Exception as e:  # noqa: BLE001 — batch sync should never crash the analyst
        print(f"[db] flush failed, re-queueing {len(batch)} records: {e}")
        with _lock:
            _buffer[:0] = batch


def start_background_flush() -> None:
    def loop():
        while True:
            time.sleep(FLUSH_SECONDS)
            _flush()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
