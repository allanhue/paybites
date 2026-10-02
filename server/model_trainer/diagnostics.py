"""
One command instead of a pile of SQL queries.

    python diagnostics.py            # everything
    python diagnostics.py --live     # only the live Redis monitor (safe to loop)

Sections
  1. Storage        - DB / table sizes (are you near the free-tier cap?)
  2. Daily outcomes - per day and kind: volume, win/loss/timeout, win rate, avg move
  3. Feature health - share of rows where macd_hist / trend_bias are NULL or exactly 0,
                      and when real trend_bias data actually started
  4. Hourly trend   - trend_bias by hour for the last 48h (is it ever negative on down days?)
  5. Kind compare   - signal vs near_miss vs gated (do the gates help?) with fee-adjusted return
  6. Live monitor   - analyst gate state + tracker rolling win rate from Redis
"""

import os
import sys

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
FEE_PCT = float(os.getenv("ROUND_TRIP_FEE_PCT", "0.20"))


def engine():
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url)


def show(title: str, df: pd.DataFrame) -> None:
    print(f"\n=== {title} ===")
    print("(no rows)" if df.empty else df.to_string(index=False))


def q(conn, sql: str, **params) -> pd.DataFrame:
    return pd.read_sql(text(sql), conn, params=params)


def db_sections() -> None:
    if not DATABASE_URL:
        print("DATABASE_URL not set - skipping database sections")
        return
    with engine().connect() as conn:
        sections = [
            ("1. STORAGE", """
                SELECT pg_size_pretty(pg_database_size(current_database())) AS database,
                       pg_size_pretty(pg_total_relation_size(to_regclass('trade_outcomes'))) AS trade_outcomes,
                       pg_size_pretty(pg_total_relation_size(to_regclass('signals'))) AS signals,
                       pg_size_pretty(pg_total_relation_size(to_regclass('news_log'))) AS news_log
            """, {}),
            ("2. DAILY OUTCOMES", """
                SELECT date_trunc('day', entry_time)::date AS day, kind,
                       COUNT(*) AS n,
                       COUNT(*) FILTER (WHERE outcome='win') AS wins,
                       COUNT(*) FILTER (WHERE outcome='loss') AS losses,
                       COUNT(*) FILTER (WHERE outcome='timeout') AS timeouts,
                       ROUND((100.0*COUNT(*) FILTER (WHERE outcome='win')
                              / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('win','loss')),0))::numeric,1) AS win_rate,
                       ROUND((AVG(ABS(pct_change))*100)::numeric,3) AS avg_abs_move_pct
                FROM trade_outcomes
                GROUP BY 1,2 ORDER BY 1 DESC, 2 LIMIT 40
            """, {}),
            ("3a. FEATURE HEALTH BY DAY", """
                SELECT date_trunc('day', entry_time)::date AS day, COUNT(*) AS n,
                       ROUND((100.0*COUNT(*) FILTER (WHERE macd_hist IS NULL OR macd_hist=0)/COUNT(*))::numeric,1) AS macd_null_or_zero_pct,
                       ROUND((100.0*COUNT(*) FILTER (WHERE trend_bias IS NULL OR trend_bias=0)/COUNT(*))::numeric,1) AS trend_null_or_zero_pct
                FROM trade_outcomes GROUP BY 1 ORDER BY 1 DESC LIMIT 14
            """, {}),
            ("3b. FIRST REAL trend_bias / macd_hist ROW", """
                SELECT MIN(entry_time) FILTER (WHERE trend_bias IS NOT NULL AND trend_bias<>0) AS first_real_trend_bias,
                       MIN(entry_time) FILTER (WHERE macd_hist IS NOT NULL AND macd_hist<>0) AS first_real_macd_hist
                FROM trade_outcomes
            """, {}),
            ("4. trend_bias BY HOUR (last 48h)", """
                SELECT date_trunc('hour', entry_time) AS hour, COUNT(*) AS n,
                       ROUND(MIN(trend_bias)::numeric,5) AS min_tb,
                       ROUND(AVG(trend_bias)::numeric,5) AS avg_tb,
                       ROUND(MAX(trend_bias)::numeric,5) AS max_tb,
                       ROUND((100.0*COUNT(*) FILTER (WHERE trend_bias<0)/COUNT(*))::numeric,1) AS pct_negative,
                       ROUND((100.0*COUNT(*) FILTER (WHERE outcome='win')
                              / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('win','loss')),0))::numeric,1) AS win_rate
                FROM trade_outcomes
                WHERE entry_time > now() - interval '48 hours' AND trend_bias IS NOT NULL AND trend_bias<>0
                GROUP BY 1 ORDER BY 1 DESC
            """, {}),
            ("5b. SCORE vs FORWARD RETURN (unbiased 'sample' rows; avg_fwd_pct must exceed the fee to matter)", """
                SELECT (floor(confidence/5)*5)::int AS score_bucket, COUNT(*) AS n,
                       ROUND((AVG(pct_change)*100)::numeric,3) AS avg_fwd_pct,
                       ROUND((STDDEV(pct_change)*100)::numeric,3) AS sd_pct,
                       ROUND((100.0*COUNT(*) FILTER (WHERE outcome='win')
                              / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('win','loss')),0))::numeric,1) AS win_rate,
                       ROUND((100.0*COUNT(*) FILTER (WHERE outcome='timeout')/COUNT(*))::numeric,1) AS timeout_pct
                FROM trade_outcomes
                WHERE kind='sample' AND outcome IS NOT NULL AND pct_change IS NOT NULL
                GROUP BY 1 ORDER BY 1
            """, {}),
            ("5. KIND COMPARISON (last 7 days, fees included)", """
                SELECT kind, COALESCE(gate,'-') AS gate, COUNT(*) AS n,
                       ROUND((100.0*COUNT(*) FILTER (WHERE outcome='win')
                              / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('win','loss')),0))::numeric,1) AS win_rate,
                       ROUND((AVG(pct_change)*100 - :fee)::numeric,3) AS mean_net_pct
                FROM trade_outcomes
                WHERE entry_time > now() - interval '7 days' AND outcome IS NOT NULL
                GROUP BY 1,2 ORDER BY 1,2
            """, {"fee": FEE_PCT}),
        ]
        for title, sql, params in sections:
            try:
                show(title, q(conn, sql, **params))
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                print(f"\n=== {title} ===\n(failed: {e})")

    b = 0.30
    be = (b + FEE_PCT) / (2 * b) * 100
    print(f"\nBreak-even win rate at +/-{b:.2f}% barriers and {FEE_PCT:.2f}% round-trip fees: {be:.1f}%")


def live_section() -> None:
    try:
        import redis
        host, port = REDIS_ADDR.split(":")
        r = redis.Redis(host=host, port=int(port), db=0)
        for key in ("monitor:scores", "monitor:gate", "monitor:rolling", "monitor:gate_counts"):
            h = {k.decode(): v.decode() for k, v in r.hgetall(key).items()}
            print(f"\n=== 6. LIVE {key} ===")
            print("(empty - is the service running?)" if not h else "\n".join(f"{k:22s} {v}" for k, v in sorted(h.items())))
    except Exception as e:  # noqa: BLE001
        print(f"\n=== 6. LIVE MONITOR ===\n(redis unavailable: {e})")


if __name__ == "__main__":
    if "--live" not in sys.argv:
        db_sections()
    live_section()