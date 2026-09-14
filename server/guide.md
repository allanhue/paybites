# paybites — System Guide

A reference for how this system fits together, how to run it, and how to add
features without breaking the existing flow. Keep this updated as you go —
it's the map back to "why did I build it this way" six weeks from now.

---

## 1. Architecture at a glance

```
Binance WS (public, real data)
        │
        ▼
go-scanner  →  Redis "market.ticks"   (price + RSI-14 + volatility)
        │
        ▼
python-analyst  →  Redis "market.signals"   (BUY signals above threshold)
        │                    │
        │                    ▼ (batched every DB_FLUSH_SECONDS)
        │                  Neon (Postgres) — full audit trail of every
        │                  scored tick, not just the ones that fired
        ▼
go-shield  →  Redis "trade.decisions"   (approved / blocked / pending_approval)
        │
        ▼
Next.js /api/signals (SSE)  →  dashboard (ConfidenceCard, SignalFeed, RiskPanel)
```

Each arrow is a Redis Pub/Sub channel. Nothing here talks to anything else
directly — every service only knows "I read from channel X, I write to
channel Y." That's deliberate: you can kill, restart, or rewrite any one bot
without touching the others, as long as the channel contracts below don't
change shape.

## 2. Where everything lives

```
paybites/ (or paybites/ui, whichever is your Next.js root)
├── app/
│   ├── api/
│   │   ├── signals/route.ts   → SSE bridge: Redis → browser
│   │   └── mode/route.ts      → reads/writes config:trading_mode
│   ├── components/
│   │   ├── navbar/Navbar.tsx
│   │   └── ui/
│   │       ├── ConfidenceCard.tsx
│   │       ├── SignalFeed.tsx
│   │       ├── RiskPanel.tsx
│   │       └── ModeToggle.tsx
│   ├── lib/useSignalStream.ts → client hook consuming the SSE stream
│   └── page.tsx               → wires the above together
└── server/
    ├── docker-compose.yml     → optional; you're running natively via Memurai instead
    ├── go-scanner/            → Bot 1: market data + indicators
    ├── python-analyst/        → Bot 2: scoring engine
    └── go-shield/             → Bot 3: risk gate + execution stub
```

## 3. The channel contracts (don't change these shapes casually)

If you change a field name here, update it on both the publisher and every
subscriber, or things will silently stop working (JSON fields just won't
populate — no crash, no error).

**`market.ticks`** (go-scanner → python-analyst)
```json
{ "symbol": "BTCUSDT", "price": 65250.0, "rsi_14": 42.1, "volatility": 0.0021, "timestamp": 1234567890 }
```

**`market.signals`** (python-analyst → go-shield)
```json
{ "symbol": "BTCUSDT", "action": "BUY", "confidence": 81.4, "trigger_price": 65250.0 }
```

**`trade.decisions`** (go-shield → dashboard)
```json
{ "symbol": "BTCUSDT", "action": "BUY", "confidence": 81.4, "price": 65250.0, "status": "pending_approval", "reason": "co-pilot mode — awaiting manual approval", "timestamp": 1234567890 }
```

**Redis keys (not channels — these are read/written directly, not pub/sub)**
- `config:trading_mode` → `"copilot"` or `"automated"`, set by `/api/mode`, read by go-shield on every signal.
- `account:balance_usd` → not yet wired to anything real. Currently nothing sets this, so go-shield's `currentBalanceUSD()` always reads 0 and treats every trade as "low balance" (fail-safe). See §6.

## 4. Running it locally (native Windows, no Docker)

Four terminals, in order:

```powershell
# Terminal 1 — Go scanner
cd server\go-scanner
go run main.go

# Terminal 2 — Python analyst
cd server\python-analyst
.\venv\Scripts\Activate.ps1
python analyst.py

or 

$env:STRATEGY_THRESHOLD = "20"
python analyst.py

# Terminal 3 — Go shield
cd server\go-shield
go run main.go

# Terminal 4 — Next.js dashboard
npm run dev 


# Terminal 1
cd server\go-scanner; go run main.go
# Terminal 2
cd server\python-analyst; .\venv\Scripts\Activate.ps1; python analyst.py
# Terminal 3
cd server\go-shield; go run main.go
# Terminal 4 (new)
cd server\outcome_tracker; .\venv\Scripts\Activate.ps1; python tracker.py
# Terminal 5
npm run dev


```
#terminal 6 
cd C:\paybites\server\api_gateway
 go run main.go

#terminal 7
cd C:\paybites\server\mt5_scanner
python scanner.py


<!-- terminal 8 -->
.\venv\Scripts\Activate.ps1
feeds.py


cd C:\paybites
>> $env:REDIS_URL = "redis://localhost:6379"
>> npm run dev



Prereqs: Memurai running as a Windows service (`Get-Service Memurai` should
show `Running`), and a `.env` in `server/python-analyst/` (or wherever
`load_dotenv()` can find it) with `DATABASE_URL` pointing at Neon.

## 5. Environment variables reference

| Variable | Used by | Purpose |
|---|---|---|
| `REDIS_ADDR` | go-scanner, go-shield | Redis host:port (default `localhost:6379`) |
| `REDIS_URL` | Next.js API routes | Redis connection URL for ioredis |
| `DATABASE_URL` | python-analyst | Neon Postgres connection string |
| `DB_FLUSH_SECONDS` | python-analyst | How often buffered signals flush to Neon (default 15s) |
| `STRATEGY_THRESHOLD` | python-analyst | Confidence score above which a BUY signal fires (default 75.0) |
| `MAX_BALANCE_USD` | go-shield | Below this balance, strict confidence rules apply (default 10.00) |
| `MIN_CONFIDENCE_LOW_BALANCE` | go-shield | Minimum confidence required when balance is at/below the ceiling (default 85.0) |

## 6. Known gaps — things intentionally left as TODOs

Be aware of these before you assume the system is "done":

1. **`account:balance_usd` is never set.** Nothing currently syncs your real
   broker/wallet balance into Redis, so go-shield always sees balance = 0 and
   applies the strict low-balance rule to every signal. You'll want a small
   job (cron, or a goroutine in go-shield) that periodically fetches your
   real balance from your broker's API and writes it to that key.
2. **`placeBrokerOrder()` in go-shield/main.go is a stub.** It only logs
   what it would do. No real order is ever placed until you wire in your
   actual broker/exchange SDK there.
3. **`scoring.py`'s `rule_based_score()` is a heuristic, not a trained
   model.** It's explainable and real (RSI + momentum + volatility), but it
   hasn't been backtested against actual outcomes yet. Treat every signal as
   a hypothesis until you've checked the `signals` table in Neon against
   what price actually did afterward.

   
4. **No authentication on the dashboard or API routes.** Anyone who can
   reach `localhost:3000` (or wherever you deploy it) can flip
   `config:trading_mode` to `automated`. Fine for local dev; add auth before
   deploying anywhere reachable by others.

## 7. Adding a feature — checklist

Before writing code for a new feature, answer these:

- **Which bot owns this?** Go bots (scanner, shield) are for anything
  latency-sensitive or touching money/risk. Python (analyst) is for anything
  analytical/statistical. The dashboard is presentation only — it should
  never contain trading logic.
- **Does it need a new Redis channel, or does it fit an existing one?**
  Adding a field to an existing JSON payload is usually safer than inventing
  a new channel — fewer places to keep in sync.
- **Does it write to Neon?** If yes, batch it (see `db.py`'s pattern) —
  don't add a new per-tick database write anywhere.
- **Does it touch `placeBrokerOrder` or account balance?** If yes, that's
  real-money code — test thoroughly in `copilot` mode first, and consider
  adding a dry-run flag.

### Example: adding a new indicator (e.g., MACD) to Bot 1

1. Add the calculation to `rollingSeries` in `go-scanner/main.go` (follow
   the `rsi14()` / `volatility()` pattern — same receiver, same lock usage).
2. Add the field to `EnrichedTick` and to the JSON payload.
3. Add the corresponding field to `Features` in `python-analyst/scoring.py`
   and read it in `analyst.py` where the tick is unmarshaled.
4. Decide its weight in `rule_based_score()` and document why, same as the
   existing three components.
5. Add the column to the Neon `signals` table DDL in `db.py` if you want it
   logged for backtesting.

### Example: adding a new dashboard panel

1. New component goes in `app/components/ui/`.
2. If it needs new real-time data, extend `useSignalStream.ts` — either add
   a new SSE event type server-side (in `route.ts`) and a new
   `es.addEventListener(...)` client-side, or derive it from data you
   already have (`signals`/`decisions`).
3. Import and place it in `page.tsx`.

## 8. Before this ever touches real money

1. Run in `copilot` mode only, for several days, manually reviewing every
   `pending_approval` decision against what the market actually did next.
2. Query the `signals` table in Neon and check real hit-rate: of the trades
   the system would have taken, how many were actually profitable?
3. Only then wire `placeBrokerOrder` to a real broker, and keep
   `MAX_BALANCE_USD` / `MIN_CONFIDENCE_LOW_BALANCE` as a hard floor while you
   keep gathering live data — don't remove the guardrail just because it's
   working in copilot mode.

## 9. Troubleshooting log

Keep adding to this as you hit things — future you will thank you.

| Symptom | Cause | Fix |
|---|---|---|
| Dashboard stuck on "reconnecting" | API route running on Edge runtime, which `ioredis` can't use | Add `export const runtime = "nodejs";` to `route.ts` files using ioredis |
| Hydration warning mentioning `data-my-extension` | Browser extension injecting attributes into `<body>` before React loads | Add `suppressHydrationWarning` to `<body>` in `layout.tsx`; not a real bug |
| PowerShell rejects `&&` | PowerShell doesn't support bash-style chaining | Use `;` instead, or run commands on separate lines |
| `python analyst.py` → "No such file or directory" | Ran from wrong working directory | `cd` into `server/python-analyst` first |


How would you actually know when to trade?

Right now, "confidence" is just this rule-based math you've seen (RSI + momentum + band position). A single high number does not mean the trade will win — it means the current price pattern matches a hypothesis you coded in. The only way to actually know if that hypothesis holds up is the piece you already have but haven't accumulated data in yet: the outcomes panel.

Here's the real workflow for "how do I know":

Let signals fire and resolve for days/weeks (win/loss/timeout, tracked automatically).
Look at the win rate by hour chart and trade outcomes panel — not the live score. If, say, signals above 75% confidence actually won 60% of the time historically, that's evidence. If they won 45% of the time, the scoring needs rework before you trust it with money.
Right now both panels say "Nothing resolved yet" — meaning you genuinely don't have evidence either way yet. Any trade you place today is still a bet on an unproven hypothesis, not a validated edge.