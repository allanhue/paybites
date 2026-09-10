# paybites server — Go/Python multi-agent trading backend

## What changed from the original sketch

- **Bot 2's "confidence score" is no longer random.** `python-analyst/scoring.py`
  computes a real, explainable score from RSI-14 + short-window momentum +
  a volatility penalty. It's a rule-based baseline, not a trained model —
  see the docstring at the top of that file for exactly how to turn it into
  one once you've logged enough outcomes.
- **Neon writes are batched**, not per-tick. `python-analyst/db.py` buffers
  scored signals in memory and flushes the whole batch every
  `DB_FLUSH_SECONDS` (default 15s). If Neon is briefly unreachable, records
  just queue up and retry on the next flush.
- **Bot 3 never auto-executes by default.** It reads a `config:trading_mode`
  key from Redis (`copilot` or `automated`). In `copilot` mode it publishes a
  `pending_approval` decision for a human to review on the dashboard; only in
  `automated` mode does it attempt `placeBrokerOrder`. The default is
  `copilot` even if the key is missing.
- **`placeBrokerOrder` in go-shield/main.go is a stub.** It logs what it
  *would* do. Wire in your actual broker/exchange SDK there once you've
  picked one — that's the one function that moves real money, so it deserves
  its own careful review, not a generic template.

## Running it locally

```bash
cd server
cp ../.env.example .env   # fill in DATABASE_URL with your Neon connection string
docker compose up --build
```

This brings up Redis + all three bots. Watch the logs:

```bash
docker compose logs -f go-scanner python-analyst go-shield
```

## Data flow

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

## Before this touches real money

1. Run it in `copilot` mode only, for at least a few days, against your own
   eyeballs approving/rejecting each `pending_approval` decision manually.
2. Query the `signals` table in Neon and check: of the trades you'd have
   taken, how many would actually have been profitable? The rule-based score
   is a hypothesis, not a guarantee — you need your own backtest before you
   trust it with the `$10` (or any) balance.
3. Only then consider wiring `placeBrokerOrder` to a real broker and flipping
   to `automated` mode — and even then, keep `MAX_BALANCE_USD` /
   `MIN_CONFIDENCE_LOW_BALANCE` as a hard floor while you gather more live
   data.
