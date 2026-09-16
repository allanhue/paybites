"""
Search the codebase for references to trade_outcomes writes and macd_hist,
to find where (or whether) macd_hist is actually being calculated/inserted.

Usage:
    python find_macd_hist.py
    (run from anywhere - it walks ROOT_DIR below)
"""

import os

ROOT_DIR = r"C:\paybites"
PATTERNS = ["trade_outcomes", "macd_hist", "INSERT INTO trade_outcomes"]
EXCLUDE_DIRS = {"venv", ".venv", "node_modules", ".git", "__pycache__"}


def search():
    matches_by_pattern = {p: [] for p in PATTERNS}

    for dirpath, dirnames, filenames in os.walk(ROOT_DIR):
        # skip noisy/irrelevant directories in-place
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]

        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
            except Exception as e:
                print(f"  [skipped: {fpath} ({e})]")
                continue

            for lineno, line in enumerate(lines, start=1):
                for pattern in PATTERNS:
                    if pattern.lower() in line.lower():
                        matches_by_pattern[pattern].append((fpath, lineno, line.strip()))

    for pattern in PATTERNS:
        results = matches_by_pattern[pattern]
        print("=" * 60)
        print(f"PATTERN: {pattern}   ({len(results)} matches)")
        print("=" * 60)
        if not results:
            print("  (no matches found)")
        for fpath, lineno, line in results:
            print(f"  {fpath}:{lineno}")
            print(f"      {line}")
        print()


if __name__ == "__main__":
    search()