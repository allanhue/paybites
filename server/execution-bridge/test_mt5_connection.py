import os
import MetaTrader5 as mt5
from dotenv import load_dotenv

print("Script started")
load_dotenv()

terminal_path = os.getenv("MT5_TERMINAL_PATH", "")
login = int(os.getenv("MT5_LOGIN", "0"))
password = os.getenv("MT5_PASSWORD", "")
server = os.getenv("MT5_SERVER", "")

print(f"Connecting: path={terminal_path} login={login} server={server}")

result = mt5.initialize(path=terminal_path, login=login, password=password, server=server, timeout=30000)
print("initialize() returned:", result)

if not result:
    print("FAILED:", mt5.last_error())
else:
    info = mt5.account_info()
    print("Connected. Login:", info.login)
    print("Balance:", info.balance, info.currency)

mt5.shutdown()
print("Script finished")