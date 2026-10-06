"""
funding_carry_check.py - how much would a delta-neutral funding-rate carry have paid?

Idea (not a prediction): hold SPOT long + PERPETUAL short in equal size. Price moves cancel.
When the market is bullish, perp longs pay perp shorts a funding rate (usually every 8h), so
you collect it. This script reads Binance's public funding history (no API key) and measures,
per coin, how much that would have paid, how bad the worst stretches were, and what is left
after trading costs.

    python funding_carry_check.py
    python funding_carry_check.py --symbols BTCUSDT,ETHUSDT --days 1095 --hold-days 60

What it does NOT model (read before getting excited):
  - Basis / mark-price risk and liquidation risk on the short leg (needs margin buffer; a sharp
    rally can liquidate an under-margined short while spot sits unrealised).
  - Capital needed: roughly 1.2x-1.5x the notional (spot + margin for the short), so
    return on CAPITAL is lower than the % shown, which is on notional.
  - Funding turning negative: then the short PAYS. That shows up in the worst-90-day column.
  - Exchange / counterparty risk, withdrawal limits, futures account availability in your country.
  - Futures API access: Binance futures endpoints may be blocked where you live.
Costs: entering and exiting both legs. Default 0.30% total (spot 0.10% + perp 0.05%, each way).
"""

import argparse
import json
import time
import urllib.error
import urllib.request

import numpy as np
import pandas as pd

HOST = "https://fapi.binance.com"


def fetch_funding(symbol: str, days: int) -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    rows = []
    while start < end:
        url = f"{HOST}/fapi/v1/fundingRate?symbol={symbol}&startTime={start}&limit=1000"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                batch = json.loads(r.read())
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            raise RuntimeError(f"funding request failed for {symbol}: {e}")
        if not batch:
            break
        rows += batch
        start = batch[-1]["fundingTime"] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.2)
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["rate"] = df["fundingRate"].astype(float)
    return df.drop_duplicates("t").set_index("t")["rate"].to_frame()


def summarize(symbol: str, f: pd.DataFrame, hold_days: float, cost_pct: float) -> dict:
    span_days = (f.index[-1] - f.index[0]).total_seconds() / 86400
    per_day = len(f) / span_days
    ann_gross = f["rate"].mean() * per_day * 365 * 100
    win = max(2, int(round(90 * per_day)))
    roll = f["rate"].rolling(win).sum() * (365 / 90) * 100  # annualised % over each 90-day window
    trades_per_year = 365 / hold_days
    ann_cost = cost_pct * trades_per_year
    by_year = (f["rate"].groupby(f.index.year).sum() * 100).round(2).to_dict()
    return {
        "symbol": symbol, "years": round(span_days / 365, 2),
        "gross_%/yr": round(ann_gross, 2),
        "cost_%/yr": round(ann_cost, 2),
        "net_%/yr": round(ann_gross - ann_cost, 2),
        "worst_90d_ann_%": round(float(roll.min()), 2),
        "best_90d_ann_%": round(float(roll.max()), 2),
        "negative_funding_%": round(float((f["rate"] < 0).mean() * 100), 1),
        "by_year_%": by_year,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT")
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--hold-days", type=float, default=60.0, help="how long each carry position is held")
    ap.add_argument("--cost-pct", type=float, default=0.30, help="total entry+exit cost of both legs, in %%")
    args = ap.parse_args()

    out = []
    for s in args.symbols.split(","):
        print(f"downloading funding history for {s} ...")
        out.append(summarize(s, fetch_funding(s, args.days), args.hold_days, args.cost_pct))
    table = pd.DataFrame(out)
    pd.set_option("display.width", 200)
    print("\n" + table.drop(columns="by_year_%").to_string(index=False))
    print("\nTotal funding collected per calendar year (% of notional, before costs):")
    for r in out:
        print(f"  {r['symbol']:10s} {r['by_year_%']}")
    print(f"\nAssumes a {args.hold_days:.0f}-day hold and {args.cost_pct:.2f}% round-trip cost across both legs.")
    print("Read worst_90d_ann_%: if it is negative, there were stretches where the short leg PAID you nothing "
          "and you needed patience. Then divide net %/yr by ~1.3 to approximate return on actual capital.")


if __name__ == "__main__":
    main()