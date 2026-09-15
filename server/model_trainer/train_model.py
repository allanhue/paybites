"""
Trains a real logistic regression classifier on accumulated trade_outcomes
data, using the four features already computed live (rsi_14, momentum,
band_position, volatility) to predict win vs loss.

Deliberately excludes 'timeout' rows — a timeout means price never moved
enough either way, which isn't the same signal as "wrong direction". Mixing
it in as a third class would blur what we're actually trying to learn:
does this feature combination predict a real winning move?

Deliberately uses logistic regression, not a black-box model: the whole
point of this system is explainability (see scoring.py's docstring). A
regression's coefficients tell you directly "higher RSI pressure adds X to
the win-probability", the same spirit as the hand-tuned weights it's meant
to replace or validate.
"""

import os

import joblib
import pandas as pd
import psycopg
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
MODEL_OUT = os.getenv("MODEL_OUT", "trained_model.pkl")


def load_data() -> pd.DataFrame:
    query = """
        SELECT rsi_14, momentum, band_position, volatility, confidence, outcome, kind
        FROM trade_outcomes
        WHERE outcome IN ('win', 'loss')
          AND rsi_14 IS NOT NULL
          AND momentum IS NOT NULL
          AND band_position IS NOT NULL
          AND volatility IS NOT NULL
    """
    with psycopg.connect(DATABASE_URL) as conn:
        return pd.read_sql(query, conn)


def main():
    df = load_data()
    print(f"Loaded {len(df)} resolved rows with full features (win/loss only, timeouts excluded).")
    print(df["outcome"].value_counts())
    print(df["kind"].value_counts())

    if len(df) < 200:
        print("\nWARNING: fewer than 200 labeled examples. Any model trained now is likely "
              "fitting noise, not a real pattern. Consider waiting for more data before "
              "trusting these results for anything.")

    features = ["rsi_14", "momentum", "band_position", "volatility"]
    X = df[features]
    y = (df["outcome"] == "win").astype(int)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=42, stratify=y
    )

    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    model = LogisticRegression(class_weight="balanced")
    model.fit(X_train_scaled, y_train)

    y_pred = model.predict(X_test_scaled)
    y_proba = model.predict_proba(X_test_scaled)[:, 1]

    print("\n--- Test set performance ---")
    print(classification_report(y_test, y_pred, target_names=["loss", "win"]))
    print(f"ROC AUC: {roc_auc_score(y_test, y_proba):.3f}  (0.5 = coin flip, 1.0 = perfect)")

    print("\n--- Feature coefficients (on standardized features) ---")
    for feat, coef in zip(features, model.coef_[0]):
        direction = "increases" if coef > 0 else "decreases"
        print(f"  {feat}: {coef:+.3f}  ({direction} win probability)")

    joblib.dump({"model": model, "scaler": scaler, "features": features}, MODEL_OUT)
    print(f"\nSaved model to {MODEL_OUT}")


if __name__ == "__main__":
    main()