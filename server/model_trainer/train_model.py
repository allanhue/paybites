"""
Train an explainable logistic regression model on historical trade outcomes.
Reads from Aiven (DATABASE_URL in .env should point there, not Neon).

Uses TimeSeriesSplit (5 sequential folds) instead of one split, and prints
a daily win-rate regime check up front, since a single split or a pooled
average can hide real drift across days.
"""

import os

import joblib
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, average_precision_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
MODEL_OUT = os.getenv("MODEL_OUT", "cloude.pkl")
FEATURES = ["rsi_14", "momentum", "band_position", "volatility", "macd_hist", "trend_bias"]

# Set this once you've confirmed (via the hourly IS NOT NULL query) exactly
# when trend_bias started flowing cleanly — same technique used for macd_hist.
TREND_BIAS_CUTOFF = os.getenv("TREND_BIAS_CUTOFF", "")


def load_data() -> pd.DataFrame:
    db_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    print(f"[trainer] connecting to: {db_url.split('@')[-1]}")

    cutoff_clause = f"AND entry_time >= '{TREND_BIAS_CUTOFF}'" if TREND_BIAS_CUTOFF else ""

    query = f"""
        SELECT rsi_14, momentum, band_position, volatility, macd_hist, trend_bias,
               confidence, outcome, kind, entry_time
        FROM trade_outcomes
        WHERE outcome IN ('win', 'loss')
          {cutoff_clause}
          AND rsi_14 IS NOT NULL AND momentum IS NOT NULL
          AND band_position IS NOT NULL AND volatility IS NOT NULL
          AND macd_hist IS NOT NULL AND trend_bias IS NOT NULL
        ORDER BY entry_time ASC
    """
    engine = create_engine(db_url)
    with engine.connect() as conn:
        return pd.read_sql(query, conn)


def print_daily_regime_check(df: pd.DataFrame):
    print("\n==============================")
    print("DAILY WIN-RATE (regime check)")
    print("==============================")
    daily = (
        df.assign(day=df["entry_time"].dt.date)
        .groupby("day")["outcome"]
        .apply(lambda s: (s == "win").mean() * 100)
        .round(1)
    )
    print(daily)
    spread = daily.max() - daily.min()
    if spread > 15:
        print(
            f"\nWARNING: win rate swings {spread:.1f} points across days. "
            "trend_bias is meant to address exactly this — check whether its "
            "coefficient below is meaningfully non-zero, and whether the "
            "fold-to-fold AUC spread has narrowed compared to previous runs."
        )


def main():
    df = load_data()
    print(f"Loaded {len(df)} resolved rows (win/loss only, timeouts excluded).")
    print("\nOutcome distribution:")
    print(df["outcome"].value_counts())
    print("\nTrade kind distribution:")
    print(df["kind"].value_counts())

    if len(df) < 100:
        print("\nERROR: not enough data to train safely.")
        return

    print_daily_regime_check(df)
    # addded evaluation for day by daya 
    day_aucs = evaluate_by_day(df)

    X = df[FEATURES].values
    y = (df["outcome"] == "win").astype(int).values

    print("\n==============================")
    print("TIME-SERIES CROSS-VALIDATION (5 sequential folds)")
    print("==============================")
    tscv = TimeSeriesSplit(n_splits=5)
    aucs = []

    

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X[train_idx])
        X_test_scaled = scaler.transform(X[test_idx])

        model = LogisticRegression(class_weight="balanced", max_iter=2000, random_state=42)
        model.fit(X_train_scaled, y[train_idx])
        proba = model.predict_proba(X_test_scaled)[:, 1]

        auc = roc_auc_score(y[test_idx], proba)
        pr = average_precision_score(y[test_idx], proba)
        aucs.append(auc)

        fold_start = df["entry_time"].iloc[test_idx[0]]
        fold_end = df["entry_time"].iloc[test_idx[-1]]
        print(f"Fold {fold + 1}: AUC={auc:.4f}  PR-AUC={pr:.4f}  "
              f"(train n={len(train_idx)}, test n={len(test_idx)}, {fold_start} → {fold_end})")

    mean_auc = sum(aucs) / len(aucs)
    print(f"\nMean AUC across folds: {mean_auc:.4f}")
    print(f"AUC range across folds: {min(aucs):.4f} to {max(aucs):.4f}")

    if mean_auc < 0.55:
        print(
            "\nVERDICT: mean AUC is not meaningfully above chance yet. Compare this fold-spread "
            "against the pre-trend_bias run — if the spread narrowed, trend_bias helped even if "
            "not enough on its own yet. If the spread is just as wide, regime drift needs a "
            "different fix (e.g. retraining on a rolling window instead of pooling all history)."
        )

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    final_model = LogisticRegression(class_weight="balanced", max_iter=2000, random_state=42)
    final_model.fit(X_scaled, y)

    print("\n==============================")
    print("FEATURE COEFFICIENTS (full-data fit, for inspection only)")
    print("==============================")
    for feature, coef in zip(FEATURES, final_model.coef_[0]):
        direction = "increases" if coef > 0 else "decreases"
        print(f"{feature:20s} {coef:+.4f} ({direction} win probability)")

    artifact = {
        "model": final_model, "scaler": scaler, "features": FEATURES,
        "rows_used": len(df), "cv_mean_auc": mean_auc, "cv_fold_aucs": aucs,
        "trustworthy": len(day_aucs) >= 3 and min(day_aucs) > 0.5 and sum(day_aucs) / len(day_aucs) >= 0.55,
        "period": {"start": str(df["entry_time"].min()), "end": str(df["entry_time"].max())},
    }
    joblib.dump(artifact, MODEL_OUT)
    print(f"\nSaved model to: {MODEL_OUT} (trustworthy={artifact['trustworthy']})")


def evaluate_by_day(df):
    df = df.copy()
    df["day"] = df["entry_time"].dt.date
    days = sorted(df["day"].unique())
    aucs = []
    print("\nEXPANDING-WINDOW EVALUATION (train on prior days, test on next day)")
    for i in range(2, len(days)):
        train = df[df["day"].isin(days[:i])]
        test = df[df["day"] == days[i]]
        yte = (test["outcome"] == "win").astype(int).values
        if len(set(yte)) < 2:
            continue
        ytr = (train["outcome"] == "win").astype(int).values
        scaler = StandardScaler()
        Xtr = scaler.fit_transform(train[FEATURES].values)
        Xte = scaler.transform(test[FEATURES].values)
        m = LogisticRegression(class_weight="balanced", max_iter=2000, random_state=42).fit(Xtr, ytr)
        auc = roc_auc_score(yte, m.predict_proba(Xte)[:, 1])
        aucs.append(auc)
        print(f"  {days[i]}: AUC={auc:.4f}  base_win_rate={yte.mean():.3f}  n={len(test)}")
    return aucs

if __name__ == "__main__":
    main()