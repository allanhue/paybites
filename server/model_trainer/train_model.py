"""
Trainer v2 - explainable logistic regression, judged honestly.

What changed vs v1
  1. Contaminated feature rows are dropped. Before the scanner rollout the
     analyst wrote macd_hist / trend_bias as 0.0 (not NULL), so `IS NOT NULL`
     did not isolate real data. Rows where these are exactly 0.0 are removed
     (NONZERO_FEATURES) and the count is printed so you can see the damage.
  2. Thinning. Near-miss rows were logged per tick, so 100k rows are a few
     hundred independent situations. We keep one row per symbol per
     THIN_MINUTES bucket. The row count after thinning is your real sample size.
  3. Purged walk-forward by day: train only on rows that entered at least
     EMBARGO_MINUTES before the test day starts (labels take up to the hold
     time to resolve, so anything closer leaks the future into training).
  4. Days with fewer than MIN_CLASS wins or losses are reported but NOT scored.
     (2026-09-30 had ~12 wins: its AUC of 0.87 was noise, not skill.)
  5. Fee-aware. Prints the break-even win rate and the mean NET return of the
     model's top-ranked picks, not just AUC.
  6. Optional gradient-boosting comparison (COMPARE_GBM=1). If it doesn't beat
     logistic regression out-of-sample, stay with the explainable model.
  7. `trustworthy` now needs enough valid days, mean AUC, day-consistency and
     top-decile lift, not one number.

Env knobs: TRAIN_KINDS, THIN_MINUTES, EMBARGO_MINUTES, MIN_TRAIN_DAYS, MIN_CLASS,
ROUND_TRIP_FEE_PCT, NONZERO_FEATURES, TREND_BIAS_CUTOFF, COMPARE_GBM, MODEL_OUT.
"""

import os

import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
MODEL_OUT = os.getenv("MODEL_OUT", "cloude.pkl")
FEATURES = ["rsi_14", "momentum", "band_position", "volatility", "macd_hist", "trend_bias"]
NONZERO_FEATURES = [f for f in os.getenv("NONZERO_FEATURES", "macd_hist,trend_bias").split(",") if f]
TRAIN_KINDS = [k for k in os.getenv("TRAIN_KINDS", "near_miss,signal").split(",") if k]
# Only train on rows entered at/after this timestamp. Set it to the moment the scanner
# started sending candle-based momentum/band_position (older rows used a different
# definition of those two features and must not be mixed in).
TREND_BIAS_CUTOFF = os.getenv("TRAIN_CUTOFF") or os.getenv("TREND_BIAS_CUTOFF", "")

THIN_MINUTES = int(os.getenv("THIN_MINUTES", "5"))
EMBARGO_MINUTES = int(os.getenv("EMBARGO_MINUTES", "30"))
MIN_TRAIN_DAYS = int(os.getenv("MIN_TRAIN_DAYS", "2"))
MIN_CLASS = int(os.getenv("MIN_CLASS", "30"))
FEE = float(os.getenv("ROUND_TRIP_FEE_PCT", "0.20")) / 100
COMPARE_GBM = os.getenv("COMPARE_GBM", "1") == "1"


def load_data() -> pd.DataFrame:
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    print(f"[trainer] connecting to: {url.split('@')[-1]}")

    q = "SELECT * FROM trade_outcomes WHERE outcome IN ('win','loss') AND kind = ANY(:kinds)"
    params: dict = {"kinds": TRAIN_KINDS}
    if TREND_BIAS_CUTOFF:
        try:
            cutoff_ts = pd.Timestamp(TREND_BIAS_CUTOFF)
            if cutoff_ts.tzinfo is None:
                cutoff_ts = cutoff_ts.tz_localize("UTC")
        except (ValueError, TypeError):
            raise SystemExit(
                f"TRAIN_CUTOFF / TREND_BIAS_CUTOFF is not a valid timestamp: {TREND_BIAS_CUTOFF!r}\n"
                "Use a real value in .env, for example: TRAIN_CUTOFF=2026-10-01 00:00:00+00"
            )
        q += " AND entry_time >= :cutoff"
        params["cutoff"] = cutoff_ts.to_pydatetime()
    q += " ORDER BY entry_time ASC"

    with create_engine(url).connect() as conn:
        return pd.read_sql(text(q), conn, params=params)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    print(f"Loaded {len(df)} resolved rows (kinds={TRAIN_KINDS}).")

    n0 = len(df)
    df = df.dropna(subset=FEATURES + ["pct_change"])
    print(f"Dropped {n0 - len(df)} rows with NULL features.")

    for f in NONZERO_FEATURES:
        if f in FEATURES:
            n1 = len(df)
            df = df[df[f] != 0.0]
            print(f"Dropped {n1 - len(df)} rows where {f} == 0.0 (pre-rollout defaults / warm-up).")

    df["y"] = (df["outcome"] == "win").astype(int)
    df["net"] = df["pct_change"] - FEE
    df = df.sort_values("entry_time").reset_index(drop=True)

    if THIN_MINUTES > 0:
        n2 = len(df)
        bucket = df["entry_time"].dt.floor(f"{THIN_MINUTES}min")
        df = (
            df.assign(_b=bucket)
            .drop_duplicates(["symbol", "_b"], keep="first")
            .drop(columns="_b")
            .sort_values("entry_time")
            .reset_index(drop=True)
        )
        print(f"Thinned {n2} -> {len(df)} rows (1 per symbol per {THIN_MINUTES} min). "
              f"THIS is your effective sample size.")

    df["day"] = df["entry_time"].dt.date
    return df


def print_regime(df: pd.DataFrame) -> None:
    print("\n==============================")
    print("DAILY WIN-RATE (after cleaning + thinning)")
    print("==============================")
    g = df.groupby("day")["y"].agg(rows="count", wins="sum", win_rate=lambda s: round(s.mean() * 100, 1))
    print(g.to_string())
    spread = g["win_rate"].max() - g["win_rate"].min()
    if spread > 15:
        print(f"\nWARNING: win rate swings {spread:.1f} points across days - regime drift dominates. "
              "Judge the model by per-day results below, not by pooled numbers.")

    b = float(df["barrier_pct"].median()) if "barrier_pct" in df and df["barrier_pct"].notna().any() else 0.003
    be = (b + FEE) / (2 * b)
    print(f"\nBREAK-EVEN: with symmetric barriers of {b*100:.2f}% and {FEE*100:.2f}% round-trip fees, "
          f"a strategy needs a {be*100:.1f}% win rate just to break even (timeouts ignored).")


def make_model(kind: str):
    if kind == "gbm":
        return HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.05, max_iter=150,
            class_weight="balanced", random_state=42,
        )
    return LogisticRegression(class_weight="balanced", max_iter=2000, random_state=42)


def walk_forward(df: pd.DataFrame, kind: str):
    days = sorted(df["day"].unique())
    rows, oos_frames = [], []
    for i in range(MIN_TRAIN_DAYS, len(days)):
        day = days[i]
        test = df[df["day"] == day]
        cutoff = test["entry_time"].min() - pd.Timedelta(minutes=EMBARGO_MINUTES)
        train = df[df["entry_time"] < cutoff]
        if len(train) < 200 or train["y"].nunique() < 2 or len(test) == 0:
            continue

        scaler = StandardScaler()
        Xtr = scaler.fit_transform(train[FEATURES].values)
        Xte = scaler.transform(test[FEATURES].values)
        model = make_model(kind).fit(Xtr, train["y"].values)
        p = model.predict_proba(Xte)[:, 1]

        yte = test["y"].values
        pos, neg = int(yte.sum()), int(len(yte) - yte.sum())
        valid = pos >= MIN_CLASS and neg >= MIN_CLASS
        auc = float(roc_auc_score(yte, p)) if valid else None

        k = max(1, int(len(p) * 0.10))
        top = np.argsort(-p)[:k]
        rows.append({
            "day": day, "n": len(test), "wins": pos, "base_wr": round(yte.mean() * 100, 1),
            "auc": None if auc is None else round(auc, 4),
            "top10_wr": round(yte[top].mean() * 100, 1),
            "top10_net_pct": round(test["net"].values[top].mean() * 100, 3),
            "scored": valid,
        })
        oos_frames.append(pd.DataFrame({"p": p, "y": yte, "net": test["net"].values}))

    oos = pd.concat(oos_frames, ignore_index=True) if oos_frames else pd.DataFrame(columns=["p", "y", "net"])
    return rows, oos


def summarize(name: str, rows: list[dict]) -> dict:
    print(f"\n--- {name}: purged walk-forward (embargo {EMBARGO_MINUTES} min) ---")
    if not rows:
        print("No testable days yet.")
        return {"valid_days": 0, "mean_auc": None, "frac_above_half": 0.0, "lift_days": 0.0}
    print(pd.DataFrame(rows).to_string(index=False))
    scored = [r for r in rows if r["scored"]]
    aucs = [r["auc"] for r in scored]
    lift = [r["top10_wr"] > r["base_wr"] for r in scored]
    s = {
        "valid_days": len(scored),
        "mean_auc": float(np.mean(aucs)) if aucs else None,
        "frac_above_half": float(np.mean([a > 0.5 for a in aucs])) if aucs else 0.0,
        "lift_days": float(np.mean(lift)) if lift else 0.0,
    }
    ma = "n/a" if s["mean_auc"] is None else f"{s['mean_auc']:.4f}"
    print(f"Scored days: {s['valid_days']}  mean AUC: {ma}  "
          f"days AUC>0.5: {s['frac_above_half']*100:.0f}%  days top-10% beats base rate: {s['lift_days']*100:.0f}%")
    return s


def print_threshold_table(oos: pd.DataFrame) -> None:
    if len(oos) < 100:
        return
    print("\nOUT-OF-SAMPLE PICKS BY MODEL RANK (pooled, fees included)")
    for q in (0.50, 0.75, 0.90, 0.95):
        sel = oos[oos["p"] >= oos["p"].quantile(q)]
        print(f"  top {int(round((1 - q) * 100)):>3d}%  n={len(sel):>6d}  "
              f"win_rate={sel['y'].mean() * 100:5.1f}%  mean_net={sel['net'].mean() * 100:+.3f}%")


def main():
    df = prepare(load_data())
    if len(df) < 500:
        print("\nERROR: not enough clean, thinned rows to evaluate. Keep collecting (or lower THIN_MINUTES).")
        return

    print("\nOutcome distribution:\n", df["outcome"].value_counts().to_string())
    print("\nSymbols:\n", df["symbol"].value_counts().to_string())
    print_regime(df)

    lr_rows, lr_oos = walk_forward(df, "logreg")
    lr = summarize("LOGISTIC REGRESSION", lr_rows)
    print_threshold_table(lr_oos)

    if COMPARE_GBM:
        gb_rows, _ = walk_forward(df, "gbm")
        summarize("GRADIENT BOOSTING (comparison only)", gb_rows)

    trustworthy = bool(
        lr["valid_days"] >= 5
        and lr["mean_auc"] is not None and lr["mean_auc"] >= 0.55
        and lr["frac_above_half"] >= 0.70
        and lr["lift_days"] >= 0.60
    )

    scaler = StandardScaler()
    X = scaler.fit_transform(df[FEATURES].values)
    final = make_model("logreg").fit(X, df["y"].values)

    print("\n==============================")
    print("FEATURE COEFFICIENTS (standardized, full-data fit, inspection only)")
    print("==============================")
    for f, c in zip(FEATURES, final.coef_[0]):
        print(f"{f:16s} {c:+.4f} ({'raises' if c > 0 else 'lowers'} win probability)")

    artifact = {
        "model": final, "scaler": scaler, "features": FEATURES,
        "rows_used": len(df), "walk_forward": lr_rows, "summary": lr,
        "config": {"thin_minutes": THIN_MINUTES, "embargo_minutes": EMBARGO_MINUTES,
                   "fee": FEE, "kinds": TRAIN_KINDS, "nonzero": NONZERO_FEATURES},
        "trustworthy": trustworthy,
        "period": {"start": str(df["entry_time"].min()), "end": str(df["entry_time"].max())},
    }
    joblib.dump(artifact, MODEL_OUT)
    print(f"\nSaved model to: {MODEL_OUT} (trustworthy={trustworthy})")
    if not trustworthy:
        print("Not trustworthy yet: needs >=5 scored days, mean AUC >=0.55, AUC>0.5 on >=70% of days, "
              "and top-10% picks beating the base rate on >=60% of days.")


if __name__ == "__main__":
    main()