"""
Bot 2 scoring engine.

This replaces the `np.random.uniform(30, 95)` placeholder from the original
sketch with an actual rule-based technical score, built from three real
signals computed off the live tick stream:

  1. RSI-14 mean-reversion pressure (from Bot 1)
  2. Short-window momentum (rate of change over the last N ticks)
  3. Volatility regime (penalizes chasing signals during erratic/noisy prints)

IMPORTANT — read this before you trust it with money:
This is a heuristic baseline, not a trained model. It encodes a simple,
explainable trading idea ("buy oversold + rising momentum + normal
volatility") so you have something real to backtest against. To turn this
into an actual predictive model:
  1. Log every (features, outcome) pair this produces to Neon (see db.py).
  2. Backtest / label outcomes: did price move +X% before -Y% within N minutes?
  3. Train a classifier (XGBoost/LightGBM) on the logged features once you
     have a few thousand labeled examples, then swap `rule_based_score()`
     below for `model.predict_proba(...)`.
Skipping step 2-3 and going live on the rule-based score alone is still a
strategy with unknown live edge — treat it as a paper-trading baseline first.
"""

from collections import deque
from dataclasses import dataclass


@dataclass
class Features:
    symbol: str
    price: float
    rsi_14: float
    volatility: float
    momentum: float  # % change over the short window, e.g. 0.004 == +0.4%


class MomentumTracker:
    """Keeps a short rolling window of recent prices per symbol to compute
    momentum, independent of Go's longer RSI/volatility window."""

    def __init__(self, window: int = 10):
        self.window = window
        self._series: dict[str, deque] = {}

    def push_and_get_momentum(self, symbol: str, price: float) -> float:
        buf = self._series.setdefault(symbol, deque(maxlen=self.window))
        buf.append(price)
        if len(buf) < 2:
            return 0.0
        return (buf[-1] - buf[0]) / buf[0]


def rule_based_score(f: Features) -> float:
    """Returns a 0-100 confidence score using explainable, inspectable logic.

    Weighting (tune these as you backtest):
      - RSI oversold/overbought pressure: 45%
      - Momentum alignment:               35%
      - Volatility penalty:                20%
    """
    # 1) RSI pressure: score rises as RSI drops below 40 (oversold, buy bias)
    #    and falls as RSI rises above 60 (overbought).
    if f.rsi_14 <= 40:
        rsi_component = min(100.0, (40 - f.rsi_14) * 2.5)
    elif f.rsi_14 >= 60:
        rsi_component = 0.0
    else:
        rsi_component = 50.0  # neutral zone

    # 2) Momentum: reward positive short-term momentum aligned with the
    #    oversold-bounce thesis; scale so +1% momentum ≈ full credit.
    momentum_component = max(0.0, min(100.0, f.momentum * 100 * 100))

    # 3) Volatility penalty: extremely high volatility means the "signal"
    #    is more likely noise, so we compress the score toward neutral.
    #    volatility here is stddev of log returns; >0.004 (~0.4%) per tick
    #    on a fast feed is already choppy for this simple heuristic.
    if f.volatility > 0.004:
        vol_penalty = min(1.0, f.volatility / 0.004) * 0.5
    else:
        vol_penalty = 0.0

    raw = (rsi_component * 0.45) + (momentum_component * 0.35)
    raw *= (1.0 - vol_penalty * 0.20)

    return round(max(0.0, min(100.0, raw)), 2)
