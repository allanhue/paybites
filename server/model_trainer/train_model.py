"""
Train an explainable logistic regression model on historical trade outcomes.

Important:
- Only win/loss outcomes are used.
- Timeouts are excluded from the direction model.
- Data is ordered chronologically.
- No random train/test split is used.
- The final test set represents the most recent unseen period.
- Features are standardized using training data only.
- Model probabilities are evaluated, not just classifications.
"""

import os

import joblib
import pandas as pd
import psycopg

from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    brier_score_loss,
    classification_report,
    log_loss,
    roc_auc_score,
)
from sklearn.preprocessing import StandardScaler


load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
MODEL_OUT = os.getenv("MODEL_OUT", "trained_model.pkl")


FEATURES = ["rsi_14", "momentum", "band_position", "volatility", "macd_hist"]

def load_data() -> pd.DataFrame:
    query = """
        SELECT
            rsi_14, momentum, band_position, volatility, macd_hist,
            confidence, outcome, kind, entry_time
        FROM trade_outcomes
        WHERE outcome IN ('win', 'loss')
          AND rsi_14 IS NOT NULL
          AND momentum IS NOT NULL
          AND band_position IS NOT NULL
          AND volatility IS NOT NULL
        ORDER BY entry_time ASC
    """
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql(query, conn)


def main():

    df = load_data()

    print(
        f"Loaded {len(df)} resolved rows "
        "(win/loss only, timeouts excluded)."
    )

    print("\nOutcome distribution:")
    print(df["outcome"].value_counts())

    print("\nTrade kind distribution:")
    print(df["kind"].value_counts())

    if len(df) < 200:
        print(
            "\nWARNING: fewer than 200 labeled examples. "
            "The model may be fitting noise."
        )

    if len(df) < 100:
        print(
            "\nERROR: not enough data to train safely."
        )
        return

    # ---------------------------------------------------------
    # Target
    # ---------------------------------------------------------

    y = (df["outcome"] == "win").astype(int)

    # ---------------------------------------------------------
    # Chronological split
    #
    # Oldest 75% = training
    # Newest 25% = testing
    # ---------------------------------------------------------

    split_index = int(len(df) * 0.75)

    train_df = df.iloc[:split_index].copy()
    test_df = df.iloc[split_index:].copy()

    X_train = train_df[FEATURES]
    X_test = test_df[FEATURES]

    y_train = y.iloc[:split_index]
    y_test = y.iloc[split_index:]

    print("\nDataset split:")
    print(f"Training rows: {len(train_df)}")
    print(f"Testing rows:  {len(test_df)}")

    print(
        f"\nTraining period: "
        f"{train_df['created_at'].min()} → "
        f"{train_df['created_at'].max()}"
    )

    print(
        f"Testing period: "
        f"{test_df['created_at'].min()} → "
        f"{test_df['created_at'].max()}"
    )

    # ---------------------------------------------------------
    # Scaling
    #
    # IMPORTANT:
    # Fit scaler ONLY on training data.
    # ---------------------------------------------------------

    scaler = StandardScaler()

    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    # ---------------------------------------------------------
    # Logistic regression
    # ---------------------------------------------------------

    model = LogisticRegression(
        class_weight="balanced",
        max_iter=2000,
        random_state=42,
    )

    model.fit(X_train_scaled, y_train)

    # ---------------------------------------------------------
    # Predictions
    # ---------------------------------------------------------

    y_pred = model.predict(X_test_scaled)

    y_proba = model.predict_proba(X_test_scaled)[:, 1]

    # ---------------------------------------------------------
    # Classification performance
    # ---------------------------------------------------------

    print("\n==============================")
    print("TEST SET PERFORMANCE")
    print("==============================")

    print(
        classification_report(
            y_test,
            y_pred,
            target_names=["loss", "win"],
            digits=4,
        )
    )

    # ---------------------------------------------------------
    # Probability metrics
    # ---------------------------------------------------------

    roc_auc = roc_auc_score(y_test, y_proba)

    pr_auc = average_precision_score(y_test, y_proba)

    brier = brier_score_loss(y_test, y_proba)

    ll = log_loss(y_test, y_proba)

    accuracy = accuracy_score(y_test, y_pred)

    print("\nProbability metrics:")
    print(f"Accuracy:  {accuracy:.4f}")
    print(f"ROC AUC:   {roc_auc:.4f}")
    print(f"PR AUC:    {pr_auc:.4f}")
    print(f"Brier:     {brier:.4f}")
    print(f"Log Loss:  {ll:.4f}")

    # ---------------------------------------------------------
    # Coefficients
    # ---------------------------------------------------------

    print("\n==============================")
    print("FEATURE COEFFICIENTS")
    print("==============================")

    for feature, coef in zip(FEATURES, model.coef_[0]):

        direction = (
            "increases"
            if coef > 0
            else "decreases"
        )

        print(
            f"{feature:20s} "
            f"{coef:+.4f} "
            f"({direction} win probability)"
        )

    # ---------------------------------------------------------
    # Save everything required for live prediction
    # ---------------------------------------------------------

    artifact = {
        "model": model,
        "scaler": scaler,
        "features": FEATURES,

        "training_rows": len(train_df),
        "test_rows": len(test_df),

        "metrics": {
            "accuracy": accuracy,
            "roc_auc": roc_auc,
            "pr_auc": pr_auc,
            "brier_score": brier,
            "log_loss": ll,
        },

        "training_period": {
            "start": str(train_df["created_at"].min()),
            "end": str(train_df["created_at"].max()),
        },

        "test_period": {
            "start": str(test_df["created_at"].min()),
            "end": str(test_df["created_at"].max()),
        },
    }

    joblib.dump(artifact, MODEL_OUT)

    print(f"\nSaved model to: {MODEL_OUT}")


if __name__ == "__main__":
    main()