import os
import MetaTrader5 as mt5
from dotenv import load_dotenv

print("Script started")

load_dotenv()

terminal_path = os.getenv("MT5_TERMINAL_PATH", "")

print(f"Connecting to terminal: {terminal_path}")

# First test: connect to the already-running MT5 terminal
result = mt5.initialize(
    path=terminal_path,
    timeout=30000
)

print("initialize() returned:", result)

if not result:
    print("FAILED:", mt5.last_error())
else:
    info = mt5.account_info()

    if info is None:
        print("Connected, but account_info() returned None")
        print("ERROR:", mt5.last_error())
    else:
        print("Connected successfully!")
        print("Login:", info.login)
        print("Server:", info.server)
        print("Balance:", info.balance, info.currency)

mt5.shutdown()

print("Script finished")