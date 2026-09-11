from binance.client import Client
import os
from dotenv import load_dotenv

load_dotenv()
client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))

account = client.get_account()
print("Connected. Can trade:", account["canTrade"])
for b in account["balances"]:
    if float(b["free"]) > 0:
        print(b["asset"], b["free"])
    # add to test_binance_connection.py, or run separately
usdt = client.get_asset_balance(asset='USDT')
print("Free USDT:", usdt['free'])