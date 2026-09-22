"""
Bot 2 scoring engine — RSI-14, momentum, band position, MACD confirmation,
and now trend_bias: price's position relative to a longer (multi-hour)
moving average. Added after cross-validated training showed the model's
predictive direction flipping across days with different market trends —
none of the other features carry any information about broader regime,
only short-window local price behavior.
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
    band_position: float
    macd_hist: float = 0.0
    trend_bias: float = 0.0


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
        return max(-1.5, min(1.5, ((price - mean) / (band_range / 2))))


def score_components(f: Features) -> dict:
    if f.rsi_14 <= 40:
        rsi_component = min(100.0, (40 - f.rsi_14) * 2.5)
    elif f.rsi_14 >= 60:
        rsi_component = 0.0
    else:
        rsi_component = 50.0

    momentum_component = max(0.0, min(100.0, f.momentum * 100 * 100))

    if f.band_position <= -1.0:
        band_component = 100.0
    elif f.band_position >= 0:
        band_component = 0.0
    else:
        band_component = (-f.band_position) * 100.0

    macd_component = 100.0 if f.macd_hist > 0 else 0.0

    # Trend confirmation: an oversold dip that's happening WITHIN a broader
    # uptrend (trend_bias > 0) is a stronger mean-reversion candidate than
    # the same dip during a broader downtrend, where it might just be the
    # start of further decline rather than a bounce. Scaled modestly since
    # this is context, not a primary driver.
    trend_component = max(0.0, min(100.0, (f.trend_bias + 0.01) * 5000))

    if f.volatility > 0.004:
        vol_penalty = min(1.0, f.volatility / 0.004) * 0.5
    else:
        vol_penalty = 0.0

    return {
        "rsi_component": round(rsi_component, 2),
        "momentum_component": round(momentum_component, 2),
        "band_component": round(band_component, 2),
        "macd_component": round(macd_component, 2),
        "trend_component": round(trend_component, 2),
        "vol_penalty": round(vol_penalty, 3),
    }


def rule_based_score(f: Features) -> tuple[float, dict]:
    """Weighting:
      RSI 25% / Momentum 20% / Band 20% / MACD 20% / Trend 15%
      Volatility penalty applied last, up to -20%.
    """
    c = score_components(f)
    raw = (c["rsi_component"] * 0.25) + (c["momentum_component"] * 0.20) + \
          (c["band_component"] * 0.20) + (c["macd_component"] * 0.20) + \
          (c["trend_component"] * 0.15)
    raw *= (1.0 - c["vol_penalty"] * 0.20)
    final = round(max(0.0, min(100.0, raw)), 2)
    return final, c