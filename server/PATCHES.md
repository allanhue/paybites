# Small patches (edit in place)

## 1. `server/execution_bridge/bridge.py` - crypto orders can never fill (real bug)

go-shield and the dashboard's Approve button publish `trigger_price`, but the bridge reads `price`.
It defaults to 0, so `TRADE_USD_AMOUNT / price` raises ZeroDivisionError and every Binance order fails.

```python
# in main(), replace:
price = float(trade.get("price", 0))
# with:
price = float(trade.get("trigger_price") or trade.get("price") or 0)
```

```python
# in execute_binance(), before computing raw_qty:
if price <= 0:
    price = float(binance_client.get_symbol_ticker(symbol=symbol)["price"])
```

Two more things to fix BEFORE any automated mode, because the bridge currently only BUYs:

- No exit logic. Nothing ever sells. Add a stop / take-profit (e.g. Binance OCO order) or a
  position manager that closes after `OUTCOME_HOLD_MINUTES`.
- No duplicate guard. Add a per-symbol lock so one signal cannot open many positions:

```python
# in main(), right after `symbol = trade["symbol"]`:
if not r.set(f"exec:lock:{symbol}", "1", nx=True, ex=900):
    print(f"[bridge] {symbol}: skipped, position lock active")
    continue
```

## 2. `server/api_gateway/main.go` - add the monitor endpoint

```go
// in main(), next to the other mux.HandleFunc lines:
mux.HandleFunc("/monitor", withCORS(handleMonitor))
```

```go
func handleMonitor(w http.ResponseWriter, r *http.Request) {
	out := map[string]map[string]string{}
	for name, key := range map[string]string{
		"rolling":     "monitor:rolling",
		"gate":        "monitor:gate",
		"gate_counts": "monitor:gate_counts",
	} {
		m, err := rdb.HGetAll(r.Context(), key).Result()
		if err == nil {
			out[name] = m
		}
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(out)
}
```

`/approve` also has no authentication and publishes straight to the real-money bridge.
Before exposing the gateway beyond localhost, require a shared secret header (e.g. `X-Approve-Token`)
checked against an env var.

## 3. `app/page.tsx` - show the health panel

```tsx
import MonitorPanel from "@/app/components/ui/monitor_panel";
// ...inside <main>, under <StatusBanner scores={scores} />:
<MonitorPanel />
```

## 4. `app/analytics/page.tsx` - keep gated rows out of "real signals"

Gated rows are shadow-tracked with `kind = 'gated'`. Change the two filters to:

```tsx
setHistoricalOutcomes(rows.filter((o: any) => o.kind === "signal"));
setHistoricalMissed(rows.filter((o: any) => o.kind !== "signal"));
```

## 5. `server/go-scanner/main.go` - make trend_bias live and less sluggish

`trend_bias` is only recomputed when a candle closes and uses the previous close, so it can be a
minute stale. Compute it against the current price instead, and use a longer window
(the comment says 240 but the constant is 45, and warm-up needs only 10 candles).

```go
const longWindowSize = 120 // 2 hours of 1-min candles
```

```go
// in computeTrendBias:
if len(longCloses) < 30 { return 0.0 }
```

```go
// snapshot takes the live price:
func (c *candleAggregator) snapshot(price float64) (rsi, vol, macdHist, trendBias float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.cachedRSI, c.cachedVol, c.cachedHist, computeTrendBias(c.longCloses, price)
}
// and in runOnce: rsi, vol, macdHist, trendBias := agg.snapshot(price)
```

Restarting the scanner resets the windows, so trend_bias reads 0.0 for the first 30 minutes after
each restart. The trainer's `NONZERO_FEATURES` filter drops those rows.

## 6. Housekeeping

- `server/python-analyst/requirements.txt` lists python-binance / MetaTrader5 but the analyst imports
  `psycopg`. Replace with: `redis==5.0.8`, `psycopg[binary]==3.2.1`, `python-dotenv==1.0.1`.
- `server/mt5_scanner/scanner.py` still computes RSI on raw ticks (the bug you fixed for crypto) and
  sends no `macd_hist` / `trend_bias`. Forex scores are therefore not comparable with crypto scores.
- `server/model_trainer/requirements.txt` has `redis ==8.1.0`; use `redis==5.0.8` to match the rest,
  and add `numpy` if it is not pulled in already.

## New env vars (all optional, defaults shown)

| Service | Variable | Default |
|---|---|---|
| analyst | `SIGNAL_COOLDOWN_SECONDS` | 300 |
| analyst | `REGIME_GATE` / `REGIME_WINDOW_SECONDS` / `REGIME_FALL_PCT` / `REGIME_MAX_FALLING` | on / 900 / 0.20 / 0.67 |
| analyst | `BREAKER` / `BREAKER_MIN_TRADES` / `BREAKER_MIN_WINRATE` | on / 50 / 40.0 |
| tracker | `NEAR_MISS_COOLDOWN_SECONDS` / `GATED_COOLDOWN_SECONDS` | 120 / 300 |
| tracker | `BARRIER_MODE` (`fixed` or `vol`) / `BARRIER_VOL_K` | fixed / 1.0 |
| tracker + trainer | `ROUND_TRIP_FEE_PCT` | 0.20 |
| trainer | `THIN_MINUTES` / `EMBARGO_MINUTES` / `MIN_CLASS` / `COMPARE_GBM` | 5 / 30 / 30 / 1 |