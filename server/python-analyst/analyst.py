"""
Bot 2: The Analyst (v2)

Publishes TWO things per tick (unchanged contract):
  - "market.scores": EVERY scored tick, with full feature + component breakdown.
  - "market.signals": only ticks that cross STRATEGY_THRESHOLD AND pass the gates.

What changed vs v1
  1. Missing scanner fields are stored as None, not 0.0. The old default made
     rows written before the scanner rollout look like "trend_bias == 0.0",
     which defeated every `IS NOT NULL` cutoff in the trainer.
  2. Per-symbol signal cooldown. Before, every tick above the threshold fired a
     new signal (hundreds per minute), which would have meant hundreds of buys
     in automated mode.
  3. Market-regime gate. If most symbols in the asset class are falling over
     the last REGIME_WINDOW_SECONDS, BUY signals are suppressed. This targets
     the "everything dumps together" days (e.g. 2026-09-30, 0.3% win rate).
  4. Circuit breaker. Reads the rolling win-rate that outcome_tracker writes to
     Redis ("monitor:rolling") and suppresses signals while it is below
     BREAKER_MIN_WINRATE.
  5. Blocked signals are still published on "market.scores" with a
     `blocked_by` field, so outcome_tracker can shadow-track them (kind
     'gated') and you can measure whether the gates actually help.
  6. Gate state is written to Redis ("monitor:gate", "monitor:gate_counts")
     for the dashboard / diagnostics.py.
"""

import json
import os
import time
from collections import deque

import redis
from dotenv import load_dotenv

import db
from scoring import BandTracker, Features, MomentumTracker, rule_based_score

# Load python-analyst\.env no matter how the process was started (run_all.ps1 already exports it;
# load_dotenv never overrides variables that are already set).
load_dotenv()

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
STRATEGY_THRESHOLD = float(os.getenv("STRATEGY_THRESHOLD", "75.0"))

# THRESHOLD_MODE=percentile makes the bar adaptive: a signal fires when a score is in the top
# (100 - THRESHOLD_PERCENTILE)% of recent scores, but never below THRESHOLD_FLOOR. The rule score
# tops out near 50 (p99 ~47), so a fixed 75 can never fire. NOTE: this makes signals APPEAR; it does
# not make them profitable. Judge them on the net-of-fee numbers in the monitor.
THRESHOLD_MODE = os.getenv("THRESHOLD_MODE", "fixed").lower()  # fixed | percentile
THRESHOLD_PERCENTILE = float(os.getenv("THRESHOLD_PERCENTILE", "99.5"))
THRESHOLD_FLOOR = float(os.getenv("THRESHOLD_FLOOR", "40"))
THRESHOLD_WARMUP_SAMPLES = int(os.getenv("THRESHOLD_WARMUP_SAMPLES", "3000"))
SIGNAL_COOLDOWN_SECONDS = float(os.getenv("SIGNAL_COOLDOWN_SECONDS", "300"))

REGIME_GATE = os.getenv("REGIME_GATE", "on").lower() == "on"
REGIME_WINDOW_SECONDS = float(os.getenv("REGIME_WINDOW_SECONDS", "900"))
REGIME_FALL_PCT = float(os.getenv("REGIME_FALL_PCT", "0.20")) / 100  # -0.20% over the window counts as "falling"
REGIME_MAX_FALLING = float(os.getenv("REGIME_MAX_FALLING", "0.67"))  # block if >= this share of symbols are falling
REGIME_MIN_SYMBOLS = int(os.getenv("REGIME_MIN_SYMBOLS", "3"))

BREAKER_ON = os.getenv("BREAKER", "on").lower() == "on"
BREAKER_MIN_TRADES = int(os.getenv("BREAKER_MIN_TRADES", "50"))
BREAKER_MIN_WINRATE = float(os.getenv("BREAKER_MIN_WINRATE", "40.0"))  # percent
BREAKER_STALE_SECONDS = float(os.getenv("BREAKER_STALE_SECONDS", "3600"))

host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), db=0)

pubsub = r.pubsub()
pubsub.subscribe("market.ticks")

momentum = MomentumTracker(window=10)
bands = BandTracker(window=20)


def is_crypto(symbol: str) -> bool:
    return symbol.upper().endswith(("USDT", "BUSD", "USDC"))


class MarketRegime:
    """Share of symbols (within one asset class) that are falling over a window."""

    def __init__(self):
        self.hist: dict[str, deque] = {}
        self._cache: dict[bool, tuple[float, tuple[str, float | None]]] = {}

    def push(self, symbol: str, price: float, now: float) -> None:
        buf = self.hist.setdefault(symbol, deque())
        if buf and now - buf[-1][0] < 5:  # sample every 5s, not every trade print
            return
        buf.append((now, price))
        while buf and now - buf[0][0] > REGIME_WINDOW_SECONDS + 60:
            buf.popleft()

    def _ret(self, symbol: str, now: float):
        buf = self.hist.get(symbol)
        if not buf:
            return None
        for t, p in buf:  # oldest sample still inside the window
            if now - t <= REGIME_WINDOW_SECONDS:
                if now - t < 0.8 * REGIME_WINDOW_SECONDS:
                    return None  # not enough coverage yet
                return buf[-1][1] / p - 1.0
        return None

    def state(self, crypto: bool, now: float) -> tuple[str, float | None]:
        cached = self._cache.get(crypto)
        if cached and now - cached[0] < 1.0:
            return cached[1]
        rets = [self._ret(s, now) for s in self.hist if is_crypto(s) == crypto]
        rets = [x for x in rets if x is not None]
        if len(rets) < REGIME_MIN_SYMBOLS:
            result = ("warming", None)
        else:
            share = sum(1 for x in rets if x <= -REGIME_FALL_PCT) / len(rets)
            result = ("blocking" if share >= REGIME_MAX_FALLING else "open", share)
        self._cache[crypto] = (now, result)
        return result


class Breaker:
    """Reads outcome_tracker's rolling win-rate from Redis, refreshed every 10s."""

    def __init__(self):
        self._t = 0.0
        self._state = "no_data"

    def check(self, now: float) -> str:
        if not BREAKER_ON:
            return "off"
        if now - self._t >= 10:
            self._t = now
            try:
                raw = r.hgetall("monitor:rolling")
            except redis.RedisError:
                raw = {}
            h = {k.decode(): v.decode() for k, v in raw.items()}
            state = "no_data"
            if h:
                age = now - float(h.get("updated_at", 0))
                n = int(float(h.get("n", 0)))
                win_rate = float(h.get("win_rate", 0))
                if age > BREAKER_STALE_SECONDS or n < BREAKER_MIN_TRADES:
                    state = "no_data"
                elif win_rate < BREAKER_MIN_WINRATE:
                    state = "tripped"
                else:
                    state = "ok"
            self._state = state
        return self._state


regime = MarketRegime()
breaker = Breaker()
last_signal_ts: dict[str, float] = {}
recent_scores: deque = deque(maxlen=20000)  # every 5th scored tick, ~7 min of history
_tick_counter = 0
current_threshold = STRATEGY_THRESHOLD  # replaced by the adaptive value once enough scores exist


def gate_reason(symbol: str, now: float) -> str | None:
    if REGIME_GATE and regime.state(is_crypto(symbol), now)[0] == "blocking":
        return "regime"
    if breaker.check(now) == "tripped":
        return "breaker"
    if now - last_signal_ts.get(symbol, 0.0) < SIGNAL_COOLDOWN_SECONDS:
        return "cooldown"
    return None


def write_monitor(now: float) -> None:
    global current_threshold
    label, share = regime.state(True, now)
    s = sorted(recent_scores)
    n = len(s)

    def pick(q: float) -> float:
        return s[min(n - 1, int(q * n))]

    if THRESHOLD_MODE == "percentile" and n >= THRESHOLD_WARMUP_SAMPLES:
        current_threshold = round(max(THRESHOLD_FLOOR, pick(THRESHOLD_PERCENTILE / 100.0)), 2)

    try:
        r.hset("monitor:gate", mapping={
            "regime_crypto": label if REGIME_GATE else "off",
            "falling_share_crypto": "n/a" if share is None else f"{share:.2f}",
            "breaker": breaker.check(now),
            "threshold": current_threshold,
            "threshold_mode": THRESHOLD_MODE,
            "cooldown_seconds": SIGNAL_COOLDOWN_SECONDS,
            "updated_at": now,
        })
        if n:
            r.hset("monitor:scores", mapping={
                "n": n,
                "p50": round(pick(0.50), 1), "p90": round(pick(0.90), 1),
                "p99": round(pick(0.99), 1), "max": round(s[-1], 1),
                "pct_above_threshold": round(100.0 * sum(1 for x in s if x > current_threshold) / n, 3),
                "threshold": current_threshold,
                "updated_at": now,
            })
    except redis.RedisError:
        pass


def main() -> None:
    db.ensure_schema()
    db.start_background_flush()

    print(
        f"Bot 2 (Analyst v2) online. Threshold={STRATEGY_THRESHOLD} "
        f"cooldown={SIGNAL_COOLDOWN_SECONDS}s regime_gate={REGIME_GATE} breaker={BREAKER_ON}. "
        "Awaiting tick stream..."
    )
    last_monitor = 0.0

    for message in pubsub.listen():
        if message["type"] != "message":
            continue

        try:
            tick = json.loads(message["data"].decode("utf-8"))
        except (json.JSONDecodeError, KeyError):
            continue

        now = time.time()
        symbol = tick["symbol"]
        price = float(tick["price"])
        regime.push(symbol, price, now)

        rsi_14 = float(tick.get("rsi_14", 50.0))
        volatility = float(tick.get("volatility", 0.0))

        # Keep "missing" distinct from "zero": scoring needs a float, storage keeps None.
        raw_macd = tick.get("macd_hist")
        raw_trend = tick.get("trend_bias")
        macd_hist = float(raw_macd) if raw_macd is not None else 0.0
        trend_bias = float(raw_trend) if raw_trend is not None else 0.0

        # Candle-based momentum / band position from the scanner when present.
        # Fallback (e.g. the MT5 scanner sends neither) uses the old raw-tick trackers.
        raw_mom = tick.get("momentum")
        raw_band = tick.get("band_position")
        mom = float(raw_mom) if raw_mom is not None else momentum.push_and_get_momentum(symbol, price)
        band_pos = float(raw_band) if raw_band is not None else bands.push_and_get_band_position(symbol, price)

        features = Features(
            symbol=symbol, price=price, rsi_14=rsi_14,
            volatility=volatility, momentum=mom, band_position=band_pos,
            macd_hist=macd_hist, trend_bias=trend_bias,
        )

        score, components = rule_based_score(features)
        global _tick_counter
        _tick_counter += 1
        if _tick_counter % 5 == 0:
            recent_scores.append(score)
        crossed = score > current_threshold
        blocked_by = gate_reason(symbol, now) if crossed else None
        emit = crossed and blocked_by is None

        if crossed and blocked_by:
            try:
                r.hincrby("monitor:gate_counts", blocked_by, 1)
            except redis.RedisError:
                pass

        if emit:
            last_signal_ts[symbol] = now
            db.queue_signal({
                "symbol": symbol, "price": price, "rsi_14": rsi_14,
                "volatility": volatility, "momentum": mom,
                "macd_hist": raw_macd, "trend_bias": raw_trend,
                "confidence": score, "action": "BUY",
            })

        score_payload = {
            "symbol": symbol, "price": price, "score": score,
            "rsi_14": rsi_14, "momentum": mom, "band_position": round(band_pos, 3),
            "volatility": volatility, "macd_hist": raw_macd, "trend_bias": raw_trend,
            "components": components, "threshold": current_threshold,
            "blocked_by": blocked_by,
        }
        r.publish("market.scores", json.dumps(score_payload))

        if emit:
            signal = {
                "symbol": symbol, "action": "BUY",
                "confidence": score, "trigger_price": price,
                "rsi_14": rsi_14, "momentum": mom,
                "band_position": round(band_pos, 3), "volatility": volatility,
                "macd_hist": raw_macd, "trend_bias": raw_trend,
            }
            print(f"Signal: {symbol} BUY @ {price} (confidence {score}%)")
            r.publish("market.signals", json.dumps(signal))

        if now - last_monitor >= 5:
            last_monitor = now
            write_monitor(now)


if __name__ == "__main__":
    main()