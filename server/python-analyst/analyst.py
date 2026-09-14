"""
Bot 2: The Analyst

Now publishes TWO things per tick:
  - "market.scores": EVERY scored tick, with full feature + component
    breakdown, for the live Analyst Detail panel and outcome-tracker's
    near-miss detection.
  - "market.signals": only ticks that cross STRATEGY_THRESHOLD, for Bot 3.
"""

import json
import os

import redis

import db
from scoring import BandTracker, Features, MomentumTracker, rule_based_score

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
STRATEGY_THRESHOLD = float(os.getenv("STRATEGY_THRESHOLD", "75.0"))

host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), db=0)
pubsub = r.pubsub()
pubsub.subscribe("market.ticks")

momentum = MomentumTracker(window=10)
bands = BandTracker(window=20)


def main() -> None:
    db.ensure_schema()
    db.start_background_flush()

    print("Smart (Analyst) online. Awaiting tick stream from Go scanner...")

    for message in pubsub.listen():
        if message["type"] != "message":
            continue

        try:
            tick = json.loads(message["data"].decode("utf-8"))
        except (json.JSONDecodeError, KeyError):
            continue

        symbol = tick["symbol"]
        price = float(tick["price"])
        rsi_14 = float(tick.get("rsi_14", 50.0))
        volatility = float(tick.get("volatility", 0.0))
        mom = momentum.push_and_get_momentum(symbol, price)
        band_pos = bands.push_and_get_band_position(symbol, price)

        features = Features(
            symbol=symbol, price=price, rsi_14=rsi_14,
            volatility=volatility, momentum=mom, band_position=band_pos,
        )
        score, components = rule_based_score(features)
        action = "BUY" if score > STRATEGY_THRESHOLD else "HOLD"

        db.queue_signal({
            "symbol": symbol, "price": price, "rsi_14": rsi_14,
            "volatility": volatility, "momentum": mom,
            "confidence": score, "action": action,
        })

        # Always publish the full scored tick for live UI + near-miss tracking
        score_payload = {
            "symbol": symbol, "price": price, "score": score,
            "rsi_14": rsi_14, "momentum": mom, "band_position": round(band_pos, 3),
            "volatility": volatility, "components": components,
            "threshold": STRATEGY_THRESHOLD,
        }
        r.publish("market.scores", json.dumps(score_payload))

        if action == "BUY":
            signal = {
                "symbol": symbol, "action": "BUY",
                "confidence": score, "trigger_price": price,
            }
            print(f"Signal: {symbol} BUY @ {price} (confidence {score}%)")
            r.publish("market.signals", json.dumps(signal))


if __name__ == "__main__":
    main()