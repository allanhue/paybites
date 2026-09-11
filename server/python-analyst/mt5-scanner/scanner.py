"""
Forex equivalent of go-scanner, since MT5's API only exists in Python.
Polls MT5 for live ticks (no native pub/sub in the MT5 API, so we poll fast),
computes the same RSI-14 + volatility used for crypto, and publishes to the
same "market.ticks" channel so python-analyst treats forex and crypto
identically.
"""

import json
import math
import os
import time
from collections import deque

import MetaTrader5 as mt5
import redis
from dotenv import load_dotenv

load_dotenv()

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
SYMBOLS = os.getenv("FOREX_SYMBOLS", "EURUSD,GBPUSD").split(",")
POLL_SECONDS = float(os.getenv("FOREX_POLL_SECONDS", "1.0"))

RSI_PERIOD = 14
WINDOW = 60

host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), db=0)

series: dict[str, deque] = {s: deque(maxlen=WINDOW) for s in SYMBOLS}


def rsi14(prices: deque) -> float:
    if len(prices) < RSI_PERIOD + 1:
        return 50.0
    gains = losses = 0.0
    p = list(prices)[-(RSI_PERIOD + 1):]
    for i in range(1, len(p)):
        delta = p[i] - p[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    if losses == 0:
        return 100.0
    rs = (gains / RSI_PERIOD) / (losses / RSI_PERIOD)
    return 100.0 - (100.0 / (1.0 + rs))


def volatility(prices: deque) -> float:
    if len(prices) < 3:
        return 0.0
    p = list(prices)
    returns = [math.log(p[i] / p[i - 1]) for i in range(1, len(p)) if p[i - 1] != 0]
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((x - mean) ** 2 for x in returns) / len(returns)
    return math.sqrt(var)


def main():
    if not mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    print(f"Forex scanner online. Watching {SYMBOLS} on server {MT5_SERVER}")

    for s in SYMBOLS:
        mt5.symbol_select(s, True)

    try:
        while True:
            for symbol in SYMBOLS:
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    continue
                mid = (tick.bid + tick.ask) / 2
                series[symbol].append(mid)

                payload = {
                    "symbol": symbol,
                    "price": mid,
                    "rsi_14": rsi14(series[symbol]),
                    "volatility": volatility(series[symbol]),
                    "timestamp": int(time.time() * 1000),
                }
                r.publish("market.ticks", json.dumps(payload))
            time.sleep(POLL_SECONDS)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()