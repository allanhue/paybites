"""
edge_lab.py - train and honestly test models on years of history, for Binance OR Exness (MT5).

Why this exists
  Your captured live data is ~2,000 independent rows selected by the old score at a 15-minute
  horizon. History has 100x more data across several market regimes, and costs can be modelled
  exactly: Binance = ~0.20% round trip; Exness = the REAL recorded spread of every bar (+ optional
  commission), which is far smaller as a share of price moves.

What it does
  1. Downloads OHLC history (Binance public API, no key; or MT5 copy_rates_range, uses the terminal
     you already run). Cached to ./edge_cache so re-runs are instant.
  2. Builds ~20 features per bar (multi-scale returns, RSI, band, MACD, trend, volatility, range,
     hour-of-day, cross-asset market return and relative strength). Nothing looks into the future.
  3. Target = forward return over H bars, MINUS the round-trip cost of that bar.
  4. Purged walk-forward test: the data is cut into time blocks; each block is predicted by a model
     trained only on EARLIER data (minus an embargo of H bars so labels cannot leak).
  5. Trades the top / bottom q of predictions in each block (shorts only if --short; Exness yes,
     Binance spot no). Reports net return after costs, t-stat (also cluster-adjusted for the number
     of symbols), fold consistency, information coefficient, and the excess over just holding.
  6. Saves a deployable model ONLY if it passes every pre-set bar. Otherwise it tells you plainly
     that there is no edge, and saves nothing.

Examples (PowerShell, from server\\model_trainer with the venv active)
  python edge_lab.py --source binance --interval 1h --horizons 4,24
  python edge_lab.py --source mt5 --interval 15m --horizons 1,4,16 --days 730
  python edge_lab.py --source mt5 --interval 1h --symbols EURUSD,XAUUSD --cost-bps 0.7

Costs
  binance : --cost-bps is the whole round trip (default 20 = 0.20%). With BNB discount + limit
            orders use ~15. Taker market orders both ways = 20.
  mt5     : cost per bar = that bar's recorded spread (as % of price) + --cost-bps (extra, default 0).
            Raw/Zero accounts charge commission: roughly 0.7 bps round trip on EURUSD at $7/lot.
            Check your account type and pass it. Standard accounts: spread only.

PASS bars (fixed in advance, do not tune them after seeing results)
  >= 300 independent trades, mean net > 0, excess over buy-and-hold > 0, cluster-adjusted
  t >= 2.5, >= 70% of folds positive, mean IC > 0.01.
  You are testing several horizons x models, so treat a single borderline PASS with suspicion;
  the real confirmation is a forward paper run on data the model has never seen.
"""

import argparse
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

load_dotenv()

BAR_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
DEFAULT_SYMBOLS = {
    "binance": "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,LTCUSDT,DOTUSDT,TRXUSDT",
    "mt5": "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF,XAUUSD",
}
FEATURES = [
    "ret_1", "ret_3", "ret_6", "ret_12", "ret_24",
    "rsi_14", "band_pos", "macd_hist", "trend_bias",
    "vol_24", "vol_ratio", "range_12",
    "hour_sin", "hour_cos",
    "mkt_ret_6", "mkt_ret_24", "rel_ret_6", "rel_ret_24",
]
CACHE_DIR = "edge_cache"


# ----------------------------------------------------------------------------- data
def _binance_get(path: str):
    last = None
    for host in BINANCE_HOSTS:
        try:
            with urllib.request.urlopen(host + path, timeout=30) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            last = e
    raise RuntimeError(f"all Binance hosts failed for {path}: {last}")


def fetch_binance(symbol: str, interval: str, days: int) -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    step = BAR_MIN[interval] * 60_000
    rows = []
    while start < end:
        batch = _binance_get(f"/api/v3/klines?symbol={symbol}&interval={interval}&startTime={start}&limit=1000")
        if not batch:
            break
        rows += batch
        start = batch[-1][0] + step
        if len(batch) < 1000:
            break
        time.sleep(0.15)
    df = pd.DataFrame(rows).iloc[:, [0, 1, 2, 3, 4]]
    df.columns = ["t", "o", "h", "l", "c"]
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df[["o", "h", "l", "c"]] = df[["o", "h", "l", "c"]].astype(float)
    df = df.drop_duplicates("t").set_index("t")
    df["spread_pct"] = 0.0
    return df


_MT5_READY = False


def fetch_mt5(symbol: str, interval: str, days: int) -> pd.DataFrame:
    global _MT5_READY
    import MetaTrader5 as mt5  # Windows only; pip install MetaTrader5

    if not _MT5_READY:
        kw = {}
        if os.getenv("MT5_TERMINAL_PATH"):
            kw["path"] = os.getenv("MT5_TERMINAL_PATH")
        if os.getenv("MT5_LOGIN"):
            kw.update(login=int(os.getenv("MT5_LOGIN")), password=os.getenv("MT5_PASSWORD", ""),
                      server=os.getenv("MT5_SERVER", ""))
        if not mt5.initialize(timeout=30000, **kw):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()} (is the terminal open and logged in?)")
        _MT5_READY = True

    tf = {"15m": mt5.TIMEFRAME_M15, "30m": mt5.TIMEFRAME_M30, "1h": mt5.TIMEFRAME_H1,
          "4h": mt5.TIMEFRAME_H4, "1d": mt5.TIMEFRAME_D1}[interval]
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"MT5 does not know symbol {symbol!r}. Exness cent/pro accounts add a suffix: "
                           "set MT5_SUFFIX in .env (e.g. c or m).")
    end = dt.datetime.now(dt.timezone.utc)
    rates = mt5.copy_rates_range(symbol, tf, end - dt.timedelta(days=days), end)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No history for {symbol} {interval}. In MT5: Tools > Options > Charts, set "
                           "'Max bars in chart' to unlimited, open the chart, scroll left to download history.")
    df = pd.DataFrame(rates)
    df["t"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("t")[["open", "high", "low", "close", "spread"]]
    df.columns = ["o", "h", "l", "c", "spread_pts"]
    df["spread_pct"] = df["spread_pts"] * info.point / df["c"] * 100
    return df[["o", "h", "l", "c", "spread_pct"]]


def load_ohlc(source: str, symbol: str, interval: str, days: int) -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{source}_{symbol}_{interval}_{days}_{dt.date.today()}.pkl")
    if os.path.exists(path):
        return pd.read_pickle(path)
    print(f"  downloading {symbol} ({source}, {interval}, {days}d) ...")
    df = fetch_binance(symbol, interval, days) if source == "binance" else fetch_mt5(symbol, interval, days)
    df.to_pickle(path)
    return df


# ------------------------------------------------------------------------ features
def _rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def make_features(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l = df["c"], df["h"], df["l"]
    r1 = np.log(c).diff()
    out = pd.DataFrame(index=df.index)
    for n in (1, 3, 6, 12, 24):
        out[f"ret_{n}"] = np.log(c / c.shift(n))
    out["rsi_14"] = _rsi(c)
    m20, s20 = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    out["band_pos"] = ((c - m20) / (2 * s20.replace(0, np.nan))).clip(-1.5, 1.5)
    e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    out["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()) / c
    out["trend_bias"] = c / c.rolling(100).mean() - 1
    out["vol_24"] = r1.rolling(24).std()
    out["vol_ratio"] = out["vol_24"] / r1.rolling(96).std().replace(0, np.nan)
    out["range_12"] = ((h - l) / c).rolling(12).mean()
    ang = 2 * np.pi * (df.index.hour + df.index.minute / 60.0) / 24.0
    out["hour_sin"], out["hour_cos"] = np.sin(ang), np.cos(ang)
    return out


def build_dataset(source, symbols, interval, days, horizons, cost_bps):
    bar = pd.Timedelta(minutes=BAR_MIN[interval])
    frames = []
    for s in symbols:
        df = load_ohlc(source, s, interval, days)
        f = make_features(df)
        f["symbol"] = s
        f["pos"] = np.arange(len(f))
        spread = df["spread_pct"].replace(0, np.nan).fillna(df["spread_pct"].median()) if source == "mt5" else 0.0
        f["cost_pct"] = spread + cost_bps / 100.0
        idx = df.index.to_series()
        for h in horizons:
            fwd = (df["c"].shift(-h) / df["c"] - 1) * 100
            fwd[(idx.shift(-h) - idx) > bar * h * 1.5] = np.nan  # skip windows that cross weekends/gaps
            f[f"fwd_{h}"] = fwd
        frames.append(f)
    d = pd.concat(frames).sort_index()
    for n in (6, 24):
        mkt = d.groupby(level=0)[f"ret_{n}"].transform("mean")
        d[f"mkt_ret_{n}"] = mkt
        d[f"rel_ret_{n}"] = d[f"ret_{n}"] - mkt
    return d


# ---------------------------------------------------------------------- evaluation
def make_ridge():
    return Ridge(alpha=300.0)


def make_gbm():
    return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=150,
                                         l2_regularization=1.0, min_samples_leaf=200, random_state=42)


def run_model(name, make, d, h, args, bar):
    fcol = f"fwd_{h}"
    d = d.dropna(subset=FEATURES + [fcol, "cost_pct"]).sort_index()
    times = d.index.unique()
    edges = np.linspace(0, len(times), args.folds + 2).astype(int)
    rows, sel_all, base_all, ics = [], [], [], []

    for k in range(1, args.folds + 1):
        t0, t1 = times[edges[k]], times[edges[k + 1] - 1]
        test = d[(d.index >= t0) & (d.index <= t1)]
        train = d[d.index < t0 - bar * h]  # embargo: labels of train rows end before the test block starts
        if len(train) < 2000 or len(test) < 200:
            continue
        sc = StandardScaler()
        xtr, xte = sc.fit_transform(train[FEATURES].values), sc.transform(test[FEATURES].values)
        y = train[fcol].values
        lo, hi = np.percentile(y, [1, 99])
        p = make().fit(xtr, np.clip(y, lo, hi)).predict(xte)

        fwd, cost = test[fcol].values, test["cost_pct"].values
        ic = pd.Series(p).rank().corr(pd.Series(fwd).rank())
        long_ = p >= np.quantile(p, 1 - args.top_q)
        short_ = (p <= np.quantile(p, args.top_q)) if args.short else np.zeros(len(p), bool)
        net = np.where(long_, fwd - cost, np.where(short_, -fwd - cost, np.nan))
        non = (test["pos"].values % h) == 0  # non-overlapping holds only
        sel, base = net[non & ~np.isnan(net)], (fwd - cost)[non]
        sel_all.append(sel)
        base_all.append(base)
        ics.append(ic)
        rows.append({"fold": k, "from": str(t0.date()), "to": str(t1.date()), "trades": len(sel),
                     "mean_net_%": round(sel.mean(), 4) if len(sel) else np.nan,
                     "hit_%": round((sel > 0).mean() * 100, 1) if len(sel) else np.nan,
                     "hold_net_%": round(base.mean(), 4), "IC": round(ic, 4)})

    if not rows or sum(len(s) for s in sel_all) < 30:
        return {"model": name, "horizon": h, "passed": False, "reason": "too few trades / folds", "rows": rows}
    sel, base = np.concatenate(sel_all), np.concatenate(base_all)
    mean, sd = sel.mean(), sel.std(ddof=1)
    t = mean / (sd / np.sqrt(len(sel)))
    t_adj = t / np.sqrt(d["symbol"].nunique())  # symbols move together: be conservative
    excess = mean - base.mean()
    solid = [r["mean_net_%"] for r in rows if r["trades"] >= 20]
    frac_pos = float(np.mean([m > 0 for m in solid])) if solid else 0.0
    ic_mean = float(np.nanmean(ics))
    passed = bool(len(sel) >= 300 and mean > 0 and excess > 0 and t_adj >= 2.5 and frac_pos >= 0.7 and ic_mean > 0.01)
    return {"model": name, "horizon": h, "passed": passed, "trades": int(len(sel)),
            "mean_net_%": round(float(mean), 4), "excess_vs_hold_%": round(float(excess), 4),
            "t": round(float(t), 2), "t_adj": round(float(t_adj), 2),
            "folds_positive": round(frac_pos, 2), "IC": round(ic_mean, 4), "rows": rows}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["binance", "mt5"], default="binance")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--interval", default="1h", choices=list(BAR_MIN))
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--horizons", default="4,24", help="hold length in BARS, comma separated")
    ap.add_argument("--cost-bps", type=float, default=None)
    ap.add_argument("--folds", type=int, default=8)
    ap.add_argument("--top-q", type=float, default=0.10)
    ap.add_argument("--short", action="store_true", help="also short the bottom q (default on for mt5)")
    ap.add_argument("--no-gbm", action="store_true")
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()

    if args.source == "mt5":
        args.short = True
    cost_bps = args.cost_bps if args.cost_bps is not None else (20.0 if args.source == "binance" else 0.0)
    symbols = (args.symbols or DEFAULT_SYMBOLS[args.source]).split(",")
    if args.source == "mt5":
        suffix = os.getenv("MT5_SUFFIX", "")
        symbols = [s if s.endswith(suffix) else s + suffix for s in symbols]
    horizons = [int(x) for x in args.horizons.split(",")]
    bar = pd.Timedelta(minutes=BAR_MIN[args.interval])

    print(f"source={args.source} interval={args.interval} days={args.days} horizons(bars)={horizons} "
          f"shorts={'yes' if args.short else 'no'} cost: "
          f"{'20 bps flat' if args.source == 'binance' and args.cost_bps is None else ''}"
          f"{'recorded spread + ' + str(cost_bps) + ' bps' if args.source == 'mt5' else (str(cost_bps) + ' bps' if args.cost_bps is not None else '')}")
    d = build_dataset(args.source, symbols, args.interval, args.days, horizons, cost_bps)
    print(f"{len(d)} rows, {d['symbol'].nunique()} symbols, {d.index.min().date()} -> {d.index.max().date()}, "
          f"median round-trip cost {d['cost_pct'].median():.4f}%")

    models = [("ridge", make_ridge)] + ([] if args.no_gbm else [("gbm", make_gbm)])
    results = []
    for h in horizons:
        for name, mk in models:
            r = run_model(name, mk, d, h, args, bar)
            results.append(r)
            print(f"\n=== {name.upper()} | hold {h} bars ({h * BAR_MIN[args.interval]} min) ===")
            if r["rows"]:
                print(pd.DataFrame(r["rows"]).to_string(index=False))
            if "trades" in r:
                print(f"pooled: trades={r['trades']} mean_net={r['mean_net_%']:+.4f}% excess_vs_hold={r['excess_vs_hold_%']:+.4f}% "
                      f"t={r['t']} t_adj={r['t_adj']} folds_positive={r['folds_positive']:.0%} IC={r['IC']:+.4f}")
            print("RESULT:", "PASS" if r["passed"] else "no edge (fails pre-set bars)")

    print("\n================ SUMMARY ================")
    for r in results:
        print(f"{r['model']:6s} h={r['horizon']:<3d} " + ("PASS" if r["passed"] else "fail")
              + (f"  net {r['mean_net_%']:+.3f}%  t_adj {r['t_adj']}  folds+ {r['folds_positive']:.0%}" if "trades" in r else f"  ({r['reason']})"))

    os.makedirs(args.out_dir, exist_ok=True)
    report = os.path.join(args.out_dir, f"edge_report_{args.source}_{args.interval}.json")
    with open(report, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nreport written to {report}")

    winners = [r for r in results if r["passed"]]
    if not winners:
        print("\nNO DEPLOYABLE MODEL. Nothing passed the pre-set bars, so nothing was saved. "
              "That is a real result, not a failure of the tooling.")
        return
    for r in winners:
        mk = dict(models)[r["model"]]
        dd = d.dropna(subset=FEATURES + [f"fwd_{r['horizon']}"])
        sc = StandardScaler()
        x = sc.fit_transform(dd[FEATURES].values)
        y = dd[f"fwd_{r['horizon']}"].values
        lo, hi = np.percentile(y, [1, 99])
        model = mk().fit(x, np.clip(y, lo, hi))
        out = os.path.join(args.out_dir, f"edge_model_{args.source}_{args.interval}_{r['model']}_h{r['horizon']}.pkl")
        joblib.dump({"model": model, "scaler": sc, "features": FEATURES, "horizon_bars": r["horizon"],
                     "interval": args.interval, "source": args.source, "symbols": symbols,
                     "top_q": args.top_q, "short": args.short, "result": {k: v for k, v in r.items() if k != "rows"},
                     "trained_on": f"{d.index.min()} -> {d.index.max()}"}, out)
        print(f"saved {out}  <-- run it as a forward PAPER test before any real money")


if __name__ == "__main__":
    main()