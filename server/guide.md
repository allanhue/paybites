# paybites — System Guide

Rewritten to match current reality. If this needs a big change later,
regenerate it whole rather than hand-editing — partial edits have broken
files in this project before.

## 1. Architecture

```
Binance WS (crypto)              MT5 terminal (forex/gold, Windows only)
      │                                    │
      ▼                                    ▼
go_scanner                          mt5_scanner (Python)
  builds 1-min candles per symbol      polls MT5, same candle logic
  RSI-14 + volatility + MACD hist      from closed candles, not raw ticks
      │                                    │
      └────────────┬───────────────────────┘
                    ▼
             python_analyst
      → "market.scores"  (EVERY scored tick, all symbols, all the time)
      → "market.signals" (only ticks above STRATEGY_THRESHOLD)
      → Neon "signals" table (batched)
                    │
                    ▼
                go_shield
      → "trade.decisions" (approved / blocked / pending_approval)
      → "trade.execute"   (only when approved — automated OR manual approve click)
                    │
                    ▼
           execution_bridge (Python)
      → Binance API (crypto) or MT5 (forex) — REAL ORDERS
      → "trade.executed"

outcome_tracker — watches signals + near-misses + ticks, resolves each into
  win/loss/timeout, writes full feature snapshot to Neon "trade_outcomes".

news_scanner — RSS headlines + VADER sentiment → "market.news". Display only,
  not fed into scoring yet.

model_trainer — offline script, trains a logistic regression on Neon data.
  NOT wired into live scoring. Purely for research/validation right now.

api_gateway (Go) — the ONLY thing the frontend talks to. SSE stream + /mode,
  /approve, /history, /status over plain HTTP.

Next.js dashboard — two pages: "/" (Live Ops, fast-moving) and "/analytics"
  (history/outcomes/news, slower retrospective view). Pure presentation,
  no Redis/Postgres client of its own.
```

## 2. Folder map (lowercase_underscore everywhere)

```
paybites/                    ← Next.js root (package.json lives here)
├── app/
│   ├── page.tsx              ← Live Ops
│   ├── analytics/page.tsx    ← Analytics & News
│   ├── components/
│   │   ├── navbar/navbar.tsx
│   │   └── ui/ (confidence_card, signal_feed, risk_panel, mode_toggle,
│   │            analyst_detail, opportunities, status_banner,
│   │            outcome_panel, history_chart, best_hours_chart, news_feed)
│   └── lib/use_signal_stream.ts
├── .env.local                ← NEXT_PUBLIC_API_URL=http://localhost:8090
└── server/
    ├── go_scanner/            Bot 1 — crypto candles + RSI/vol/MACD
    ├── mt5_scanner/            Bot 1b — forex/gold candles (needs MT5 open)
    ├── python_analyst/        Bot 2 — scoring
    ├── go_shield/              Bot 3 — risk gate + dispatch
    ├── execution_bridge/       real order placement (Binance + MT5)
    ├── outcome_tracker/        resolves win/loss/timeout, async DB writes
    ├── news_scanner/           RSS + sentiment
    ├── balance_sync/           real balance → Redis
    ├── model_trainer/          offline training script, NOT live
    └── api_gateway/            HTTP+SSE gateway for the frontend
```

## 3. CRITICAL gotcha: Go does not auto-load `.env` files

Only Python services use `python-dotenv` (`load_dotenv()`). **Go binaries
(`go_scanner`, `go_shield`, `api_gateway`) read only real OS environment
variables** — a `.env` file sitting next to them does nothing on its own.
This caused the "Analytics history is always empty" and "port already in
use after restart" bugs.

Two ways to fix, pick one per service:
- Add `github.com/joho/godotenv` and call `godotenv.Load()` as the first
  line of `main()` (done in `api_gateway`).
- Or load the `.env` into the PowerShell process before `go run` — see the
  `run_all.ps1` below, which does this for every Go service automatically.




## 4. Running everything — `run_all.ps1` (project root)


$ErrorActionPreference = "Stop"
Write-Host "Starting paybites services..." -ForegroundColor Cyan

$loadEnv = @'
function Load-EnvFile([string]$Path) {
  if (-not (Test-Path $Path)) { return }
  Get-Content $Path | ForEach-Object {
    if ($_ -match '^\s*$' -or $_ -match '^\s*#') { return }
    $name, $value = $_ -split '=', 2
    if ($name) {
      [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
    }
  }
}
'@

Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd C:\paybites\server\go-scanner; go run main.go"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd C:\paybites\server\mt5_scanner; .\venv\Scripts\Activate.ps1; python scanner.py"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "$loadEnv; cd C:\paybites\server\python-analyst; Load-EnvFile .env; .\venv\Scripts\Activate.ps1; python analyst.py"

Start-Process powershell -ArgumentList "-NoExit", "-Command", "$loadEnv; cd C:\paybites\server\go-shield; Load-EnvFile ..\python-analyst\.env; go run main.go"

Start-Process powershell -ArgumentList "-NoExit", "-Command", "$loadEnv; cd C:\paybites\server\outcome_tracker; Load-EnvFile ..\python-analyst\.env; .\venv\Scripts\Activate.ps1; python tracker.py"

Start-Process powershell -ArgumentList "-NoExit", "-Command", "$loadEnv; cd C:\paybites\server\news_scanner; Load-EnvFile .env; .\venv\Scripts\Activate.ps1; python scanner.py"

Start-Process powershell -ArgumentList "-NoExit", "-Command", "$loadEnv; cd C:\paybites\server\api_gateway; Load-EnvFile .env; go run main.go"

Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd C:\paybites\server\model_trainer; .\venv\Scripts\Activate.ps1; python train_model.py"

Write-Host "Core services launched. execution_bridge and balance_sync are NOT auto-started" -ForegroundColor Yellow

Write-Host "Launching dashboard..." -ForegroundColor Green
npm run dev




## 5. Redis channel contracts

**`market.ticks`**: `{symbol, price, rsi_14, volatility, macd_hist, timestamp}` — RSI/vol/MACD only change on candle close, not per raw trade.

**`market.scores`**: every scored tick — `{symbol, price, score, rsi_14, momentum, band_position, volatility, macd_hist, components, threshold}`

**`market.signals`**: only BUY-worthy — `{symbol, action, confidence, trigger_price, rsi_14, momentum, band_position, volatility, macd_hist}`

**`trade.decisions`**: `{symbol, action, confidence, price, status, reason, timestamp}` — status is `approved`/`blocked`/`pending_approval`

**`trade.execute`**: sent by go_shield (automated) or the dashboard's Approve button (manual) → execution_bridge

**`market.news`**: `{headline, link, source, sentiment, symbols, timestamp}` — display only

**Redis keys**: `config:trading_mode` (`copilot`/`automated`), `account:balance_usd` (written by `balance_sync`)

## 6. The balance-gate ordering gotcha (still true)

```go
if balance <= MAX_BALANCE_USD && confidence < MIN_CONFIDENCE_LOW_BALANCE {
    → "blocked"  // fires FIRST, before mode is even checked
}
```
At $0 balance, low-confidence signals never reach `pending_approval` regardless of Co-Pilot/Automated mode. This is the safety design working correctly, not a bug — fund accounts or raise confidence to see the Approve flow.

## 7. Data quality — hard lessons from real debugging

1. **RSI was computed on raw trade ticks, not time-based candles**, causing
   it to saturate at 0 for ~50% of crypto rows (BTC/ETH's high tick rate hit
   "all-one-direction in 14 raw prints" constantly). Fixed by rewriting
   `go_scanner` to build real 1-minute candles and compute RSI-14 over the
   last 14 candle closes. **Any training data from before this fix is
   contaminated** — filter by `entry_time` after the fix's rollout hour,
   confirmed via `WHERE macd_hist IS NOT NULL` transitioning cleanly (found
   ours at `2026-09-16 18:00:00+00`).
2. **`macd_hist` was computed but silently dropped before publishing** — it
   was used inside `Features()` for scoring but never added to the three
   outgoing payload dicts in `analyst.py`. Fixed; verify with the query in
   §9 before trusting any MACD-based training data.
3. **A single afternoon of data is not enough to train on.** A test run on
   ~3.5 hours of clean post-fix data produced ROC AUC 0.18 (worse than
   random) — the model learned a short-term market direction quirk, not a
   durable pattern. Wait for data spanning multiple days and both up/down
   conditions before retraining.
4. **`trade_outcomes` is currently ~100% `near_miss` kind, near-zero real
   `signal` kind.** A model trained on this pool mostly learns the
   near-miss detector, not real trade-signal quality. Filter to
   `kind = 'signal'` once enough real signals accumulate.

   

## 8. Before this ever touches real money

1. Copilot mode only, manually reviewing every `pending_approval` against
   what price actually did next.
2. Check `trade_outcomes` for a real win rate across enough volume AND
   varied market conditions — not one afternoon.
3. Small real test trade first (`TRADE_USD_AMOUNT`/`TRADE_LOT_SIZE` small),
   confirm the fill on the broker's own order history.
4. Never remove `MAX_BALANCE_USD`/`MIN_CONFIDENCE_LOW_BALANCE` as a floor.
5. Don't load `model_trainer`'s output into live scoring until its ROC AUC
   is consistently, meaningfully above 0.5 across multiple retrains on
   different time windows — one good run proves nothing on its own.

## 9. Diagnostic queries worth keeping

```sql
-- Outcome distribution
SELECT kind, outcome, COUNT(*) FROM trade_outcomes GROUP BY kind, outcome ORDER BY kind, outcome;

-- Find exactly when a fix's data started flowing (works for any new column)
SELECT date_trunc('hour', entry_time) AS hour,
       COUNT(*) FILTER (WHERE macd_hist IS NOT NULL) AS non_null, COUNT(*) AS total
FROM trade_outcomes GROUP BY hour ORDER BY hour;

-- RSI sanity check (should be spread out, not piled at 0)
SELECT rsi_14, COUNT(*) FROM trade_outcomes WHERE rsi_14 IS NOT NULL
GROUP BY rsi_14 ORDER BY COUNT(*) DESC LIMIT 20;
```

## 10. Troubleshooting log

| Symptom | Cause | Fix |
|---|---|---|
| Analytics history empty despite Neon having rows | Go doesn't auto-load `.env` | Use `run_all.ps1`'s Load-EnvFile, or add `godotenv.Load()` |
| `bind: address already in use` on :8090 | Old `api_gateway` still running | `Get-NetTCPConnection -LocalPort 8090`, then `Stop-Process` on that PID |
| Live panels empty, no signals firing | No score has crossed `STRATEGY_THRESHOLD` yet | Correct behavior — check the new Status Banner for the live gap |
| Model ROC AUC below 0.5 | Training window too short / one-directional market | Need multi-day data across varied conditions, not one session |
| `KeyError: 'created_at'` in model_trainer | Schema uses `entry_time`, not `created_at` | Use `entry_time` everywhere in the query and script |
| Folder rename fails, "process cannot access" | A running process has that folder open | Ctrl+C it first |
| `$env:X` sticks across unrelated runs | PowerShell session-scoped vars persist | Close and reopen the terminal |