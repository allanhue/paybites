"""
Periodically reads your real Binance spot USDT + MT5 account balance and
writes their sum to Redis "account:balance_usd", which go-shield reads on
every incoming signal. Runs forever, refreshing every SYNC_SECONDS.

Note: this treats 1 USDT ~= $1 and assumes your MT5 account is USD-denominated.
If your MT5 account currency isn't USD, the number will be off by whatever
the real exchange rate is — check `info.currency` from test_mt5_connection.py.
"""

import os
import time

import MetaTrader5 as mt5
import redis
from binance.client import Client
from dotenv import load_dotenv

load_dotenv()

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), db=0)

binance_client = Client(os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_API_SECRET", ""))

SYNC_SECONDS = float(os.getenv("BALANCE_SYNC_SECONDS", "30"))

mt5_ready = mt5.initialize(
    login=int(os.getenv("MT5_LOGIN", "0")),
    password=os.getenv("MT5_PASSWORD", ""),
    server=os.getenv("MT5_SERVER", ""),
)
if not mt5_ready:
    print(f"[balance-sync] MT5 not initialized: {mt5.last_error()} — will report $0 for forex side")


def get_binance_usdt() -> float:
    try:
        bal = binance_client.get_asset_balance(asset="USDT")
        return float(bal["free"])
    except Exception as e:
        print(f"[balance-sync] Binance balance fetch failed: {e}")
        return 0.0


def get_mt5_balance() -> float:
    if not mt5_ready:
        return 0.0
    info = mt5.account_info()
    if info is None:
        return 0.0
    return float(info.balance)


def main():
    print("Balance sync online.")
    while True:
        total = get_binance_usdt() + get_mt5_balance()
        r.set("account:balance_usd", str(round(total, 2)))
        print(f"[balance-sync] account:balance_usd = {total:.2f}")
        time.sleep(SYNC_SECONDS)


if __name__ == "__main__":
    main()