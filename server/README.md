# paybites — Go/Python multi-agent trading research system

A multi-agent pipeline that scans live crypto markets, scores opportunities with an explainable rule-based
engine, gates every trade through a risk shield, and tracks real outcomes
so the scoring can eventually be validated (or replaced) with a trained
model. Two dashboards: Live Ops and Analytics & News.

**Current status: research/paper mode.** Nothing trades automatically
unless you explicitly fund an account and flip `config:trading_mode` to
`automated` — the default is always `copilot` (human-in-the-loop approval).

## What changed from the original sketch

- **Scoring is real, not random.** `python_analyst/scoring.py` combines
  RSI-14, short-window momentum, Bollinger-style band position, and MACD
  histogram confirmation into an explainable 0-100 score. It's a rule-based
  baseline, not a trained model — see `server/model_trainer/` for the
  in-progress work to validate or replace it with something learned from
  real outcomes.
- **RSI/MACD are computed on time-based candles, not raw ticks.** An early
  version computed RSI over the last 14 raw WebSocket trade prints, which
  saturated at 0 constantly on busy symbols. Fixed by aggregating into
  1-minute candles first (see `go_scanner/main.go`).
- **Every scored tick is logged, not just fired signals.** `python_analyst`
  publishes to `market.scores` (everything) and `market.signals` (only
  BUY-worthy), and batches writes to Neon so a burst of volume never blocks
  the live pipeline.
- **Outcomes are tracked automatically.** `outcome_tracker` watches every
  signal and near-miss, resolves it into win/loss/timeout against real
  subsequent price movement, and stores the full feature snapshot — this is
  the data `model_trainer` eventually learns from.
- **`go_shield` never auto-executes by default.** Reads `config:trading_mode`
  from Redis (`copilot` or `automated`, default `copilot`). In `copilot`
  mode, a `pending_approval` decision with a 60-second expiring "Approve &
  Trade" button appears on the dashboard; nothing executes without a click.
- **`execution_bridge` places real orders** — routes to Binance API (crypto)
  or MT5 (forex) based on symbol shape. Test on Binance Testnet with
  separate testnet-only API keys before ever setting `BINANCE_TESTNET=false`.

## Running it locally

See `server/guide.md` for the full architecture reference. Quick start:
```powershell
.\run_all.ps1
```
This launches every read-only/analytical service. `execution_bridge` and
`balance_sync` (real money / real balance) are started manually and
separately, on purpose.

## Data flow

```
Binance WS + MT5 terminal
        │
        ▼
go_scanner / mt5_scanner  →  Redis "market.ticks"  (candle-based RSI/vol/MACD)
        │
        ▼
python_analyst  →  "market.scores" (all) + "market.signals" (BUY-worthy)
        │                              │
        │ (batched)                    ▼
        ▼                        outcome_tracker → Neon "trade_outcomes"
     Neon "signals"
        │
        ▼
go_shield  →  "trade.decisions"  →  "trade.execute" (on approval)
        │
        ▼
execution_bridge  →  Binance / MT5  (real orders)
```

## Before this touches real money

1. Run `copilot` mode only, for days across varied market conditions,
   manually reviewing every `pending_approval` against what price actually
   did next.
2. Check `trade_outcomes` in Neon for a genuine win rate — not from one
   afternoon or one market direction. Use the diagnostic queries in
   `server/guide.md` §9.
3. Test `execution_bridge` against Binance Testnet (separate testnet API
   keys) and, for Exness, a demo MT5 account, before any real credentials.
4. Only then consider `automated` mode, small position sizes first, and
   never remove `MAX_BALANCE_USD` / `MIN_CONFIDENCE_LOW_BALANCE` as a floor.
5. Don't trust `model_trainer`'s output for live scoring until it shows a
   consistent, meaningfully-above-0.5 ROC AUC across multiple retrains on
   different time windows — see `server/guide.md` §7 for why an early run
   scored 0.18 (worse than random) and what that taught us.