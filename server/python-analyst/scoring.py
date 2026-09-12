"""
Bot 2 scoring engine — now with two independent real signals instead of one:

  1. RSI-14 mean-reversion pressure
  2. Short-window momentum
  3. NEW: Bollinger-style band position (price relative to its own rolling
     mean +/- 2 stddev) — a second, independent mean-reversion signal.
     When price is near/below the lower band, that's classic oversold
     pressure from a different angle than RSI, so requiring both to agree
     is a stronger filter than either alone.
  4. Volatility penalty (unchanged)

Still a rule-based baseline, not a trained model — same caveat as before.
Log every score to Neon (via outcome-tracker) so you can eventually replace
this with a trained classifier once you have enough labeled outcomes.
"""

from collections import deque
from dataclasses import dataclass
import statistics


@dataclass
class Features:
    symbol: str
    price: float
    rsi_14: float
    volatility: float
    momentum: float
    band_position: float  # -1 = at lower band, 0 = at mean, +1 = at upper band


class MomentumTracker:
    def __init__(self, window: int = 10):
        self.window = window
        self._series: dict[str, deque] = {}

    def push_and_get_momentum(self, symbol: str, price: float) -> float:
        buf = self._series.setdefault(symbol, deque(maxlen=self.window))
        buf.append(price)
        if len(buf) < 2:
            return 0.0
        return (buf[-1] - buf[0]) / buf[0]


class BandTracker:
    """Rolling mean + stddev over a longer window than momentum, used to
    compute where price sits relative to its own recent Bollinger-style band."""

    def __init__(self, window: int = 20):
        self.window = window
        self._series: dict[str, deque] = {}

    def push_and_get_band_position(self, symbol: str, price: float) -> float:
        buf = self._series.setdefault(symbol, deque(maxlen=self.window))
        buf.append(price)
        if len(buf) < 5:
            return 0.0
        mean = statistics.fmean(buf)
        stdev = statistics.pstdev(buf) or 1e-9
        upper = mean + 2 * stdev
        lower = mean - 2 * stdev
        band_range = upper - lower or 1e-9
        # Normalize to roughly -1..+1: -1 at/below lower band, +1 at/above upper
        return max(-1.5, min(1.5, ((price - mean) / (band_range / 2))))


def score_components(f: Features) -> dict:
    """Returns each sub-component separately so the dashboard can show the
    breakdown, not just the final number."""

    if f.rsi_14 <= 40:
        rsi_component = min(100.0, (40 - f.rsi_14) * 2.5)
    elif f.rsi_14 >= 60:
        rsi_component = 0.0
    else:
        rsi_component = 50.0

    momentum_component = max(0.0, min(100.0, f.momentum * 100 * 100))

    # Band component: reward being at/below the lower band (band_position <= -1),
    # same 0-100 scale as the others.
    if f.band_position <= -1.0:
        band_component = 100.0
    elif f.band_position >= 0:
        band_component = 0.0
    else:
        band_component = (-f.band_position) * 100.0

    if f.volatility > 0.004:
        vol_penalty = min(1.0, f.volatility / 0.004) * 0.5
    else:
        vol_penalty = 0.0

    return {
        "rsi_component": round(rsi_component, 2),
        "momentum_component": round(momentum_component, 2),
        "band_component": round(band_component, 2),
        "vol_penalty": round(vol_penalty, 3),
    }


def rule_based_score(f: Features) -> tuple[float, dict]:
    """Weighting (tune as you backtest):
      - RSI pressure:    35%
      - Momentum:        30%
      - Band position:   25% (new)
      - Volatility penalty applied last, up to -20%
    Returns (final_score, components_dict) so callers can log/display both.
    """
    c = score_components(f)
    raw = (c["rsi_component"] * 0.35) + (c["momentum_component"] * 0.30) + (c["band_component"] * 0.25)
    raw *= (1.0 - c["vol_penalty"] * 0.20)
    final = round(max(0.0, min(100.0, raw)), 2)
    return final, c