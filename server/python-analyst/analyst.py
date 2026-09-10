"""
Bot 2: The Analyst

Subscribes to "market.ticks" (published by the Go scanner), computes a real
rule-based confidence score per tick (see scoring.py), publishes anything
above the strategy threshold to "market.signals" for Bot 3, and queues every
scored tick for a batched Neon write (see db.py).
"""

import json
import os

from dotenv import load_dotenv
load_dotenv()

import redis

import db
from scoring import Features, MomentumTracker, rule_based_score

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
STRATEGY_THRESHOLD = float(os.getenv("STRATEGY_THRESHOLD", "75.0"))

host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), db=0)
pubsub = r.pubsub()
pubsub.subscribe("market.ticks")

momentum = MomentumTracker(window=10)


def main() -> None:
    db.ensure_schema()
    db.start_background_flush()

    print("Bot 2 (Analyst) online. Awaiting tick stream from Go scanner...")

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

        features = Features(
            symbol=symbol, price=price, rsi_14=rsi_14,
            volatility=volatility, momentum=mom,
        )
        score = rule_based_score(features)
        action = "BUY" if score > STRATEGY_THRESHOLD else "HOLD"

        db.queue_signal({
            "symbol": symbol, "price": price, "rsi_14": rsi_14,
            "volatility": volatility, "momentum": mom,
            "confidence": score, "action": action,
        })

        if action == "BUY":
            signal = {
                "symbol": symbol,
                "action": "BUY",
                "confidence": score,
                "trigger_price": price,
            }
            print(f"Signal: {symbol} BUY @ {price} (confidence {score}%)")
            r.publish("market.signals", json.dumps(signal))


if __name__ == "__main__":
    main()
