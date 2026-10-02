"""
Horizon backtest - do your indicator ideas predict returns at longer holds?

Downloads ~2 years of hourly candles from Binance's free public market-data API
(no API key needed), builds the same kinds of features your live system uses
(RSI-14, momentum, band position, MACD histogram, trend bias) on HOURLY candles,
and measures what the price did over the next 1h / 4h / 24h.

For every feature and horizon it prints the average forward return (in %) for
the 5 quintiles of that feature, the Q5-Q1 spread with a t-statistic, and
whether the spread has the same sign in the first and second half of the data.

Run:   python horizon_backtest.py
Env:   BT_SYMBOLS, BT_DAYS (default 730), BT_HORIZONS_H (default 1,4,24),
       ROUND_TRIP_FEE_PCT (default 0.20)

How to read it (be skeptical - there are ~15 spreads here, so 1-2 will look
"significant" by luck):
  - A feature only matters if the best quintile beats the fee,
    |t| > 3, and the spread has the same sign in BOTH halves.
  - Symbols move together, so real significance is weaker than the t-stat says.
  - The six coins are today's survivors, which flatters results. If nothing
    clears the bar even here, it won't live.
"""

import json
import os
import time
import urllib.error
import urllib.request

import numpy as np
import pandas as pd

SYMBOLS = os.getenv("BT_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT").split(",")
DAYS = int(os.getenv("BT_DAYS", "730"))
HORIZONS = [int(x) for x in os.getenv("BT_HORIZONS_H", "1,4,24").split(",")]
FEE_PCT = float(os.getenv("ROUND_TRIP_FEE_PCT", "0.20"))
HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]


def _get(path: str):
    last = None
    for host in HOSTS:
        try:
            with urllib.request.urlopen(host + path, timeout=30) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            last = e
    raise RuntimeError(f"all hosts failed for {path}: {last}")


def fetch_closes(symbol: str) -> pd.Series:
    end = int(time.time() * 1000)
    start = end - DAYS * 86400 * 1000
    rows = []
    while start < end:
        batch = _get(f"/api/v3/klines?symbol={symbol}&interval=1h&startTime={start}&limit=1000")
        if not batch:
            break
        rows += batch
        start = batch[-1][0] + 3600_000
        if len(batch) < 1000:
            break
        time.sleep(0.2)
    df = pd.DataFrame(rows).iloc[:, [0, 4]]
    df.columns = ["t", "c"]
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df["c"] = df["c"].astype(float)
    return df.drop_duplicates("t").set_index("t")["c"]


def build(symbol: str, c: pd.Series) -> pd.DataFrame:
    d = c.diff()
    gain = d.clip(lower=0).rolling(14).mean()
    loss = (-d.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + gain / loss)

    mean20, sd20 = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    band = ((c - mean20) / (2 * sd20)).clip(-1.5, 1.5)

    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_hist = (macd - macd.ewm(span=9, adjust=False).mean()) / c

    out = pd.DataFrame({
        "rsi_14": rsi,
        "momentum": c / c.shift(10) - 1,
        "band_position": band,
        "macd_hist": macd_hist,
        "trend_bias": c / c.rolling(120).mean() - 1,
    })
    for h in HORIZONS:
        out[f"fwd_{h}"] = (c.shift(-h) / c - 1) * 100
    out["pos"] = np.arange(len(out))
    out["symbol"] = symbol
    return out


def add_composite(df: pd.DataFrame) -> pd.DataFrame:
    """Rank-average using the SAME directions your rule score rewards:
    low RSI, low band position, high momentum, positive MACD, price above trend."""
    r = lambda s: s.rank(pct=True)
    df["rule_direction_composite"] = (
        r(-df["rsi_14"]) + r(-df["band_position"]) + r(df["momentum"])
        + r(df["macd_hist"]) + r(df["trend_bias"])
    ) / 5
    return df


def evaluate(df: pd.DataFrame, h: int) -> None:
    sample = df[df["pos"] % h == 0].dropna(subset=[f"fwd_{h}"]).copy()  # non-overlapping holds
    sample = sample.sort_index()
    half = sample.index[len(sample) // 2]
    fcol = f"fwd_{h}"
    print(f"\n=== HOLD {h}h | non-overlapping samples: {len(sample)} "
          f"| unconditional mean {sample[fcol].mean():+.3f}% (sd {sample[fcol].std():.2f}%) "
          f"| fee {FEE_PCT:.2f}% ===")
    print(f"{'feature':26s}{'Q1':>8s}{'Q2':>8s}{'Q3':>8s}{'Q4':>8s}{'Q5':>8s}{'Q5-Q1':>9s}{'t':>7s}  halves")
    feats = ["rsi_14", "momentum", "band_position", "macd_hist", "trend_bias", "rule_direction_composite"]
    for f in feats:
        s = sample.dropna(subset=[f])
        try:
            q = pd.qcut(s[f], 5, labels=False, duplicates="drop")
        except ValueError:
            continue
        means = [s.loc[q == i, fcol].mean() for i in range(5)]
        a, b = s.loc[q == 4, fcol], s.loc[q == 0, fcol]
        spread = a.mean() - b.mean()
        t = spread / np.sqrt(a.var() / len(a) + b.var() / len(b))

        def half_spread(part):
            qq = pd.qcut(part[f], 5, labels=False, duplicates="drop")
            return part.loc[qq == 4, fcol].mean() - part.loc[qq == 0, fcol].mean()

        h1, h2 = half_spread(s[s.index < half]), half_spread(s[s.index >= half])
        same = "same sign" if h1 * h2 > 0 else "DIFFERENT"
        print(f"{f:26s}" + "".join(f"{m:+8.3f}" for m in means) + f"{spread:+9.3f}{t:+7.1f}  {same} ({h1:+.2f}/{h2:+.2f})")
    print(f"(a quintile only matters if its mean exceeds +{FEE_PCT:.2f}%, the round-trip fee)")


def main():
    frames = []
    for s in SYMBOLS:
        print(f"downloading {s} ...")
        frames.append(build(s, fetch_closes(s)))
    df = add_composite(pd.concat(frames)).sort_index()
    print(f"\n{len(df)} hourly rows across {len(SYMBOLS)} symbols, "
          f"{df.index.min().date()} to {df.index.max().date()}")
    for h in HORIZONS:
        evaluate(df, h)
    print("\nVerdict rule: nothing here counts unless a quintile mean exceeds the fee, |t| > 3, "
          "and both halves agree. Expect most rows to fail - that is the point of the test.")


if __name__ == "__main__":
    main()