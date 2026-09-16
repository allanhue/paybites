Fixed it, bro. The news scanner was publishing live headlines correctly, but Redis Pub/Sub has no memory, so analytics would show Waiting for headlines... if the page connected after those headlines already fired.
What changed:
- [scanner.py](C:/paybites/server/news_scanner/scanner.py): now stores latest headlines in Redis list news:latest and still publishes market.news.
- [main.go](C:/paybites/server/api_gateway/main.go): /events now replays the latest cached news to every new dashboard connection before streaming live updates.
- [use_signal_stream.ts](C:/paybites/app/lib/use_signal_stream.ts): added safer API URL fallback and defensive market.news parsing.
- [news_feed.tsx](C:/paybites/app/components/ui/news_feed.tsx): made rendering tolerant of missing link/source and fixed the import path.
Verified:


Checked it end to end. I did not change trading/scoring functionality. This turn I only added troubleshooting notes to [server/guide.md](C:/paybites/server/guide.md).
Main causes:
1. Analytics history is empty because the live API gateway is not loading DATABASE_URL.
   http://localhost:8090/history?symbol=BTCUSDT returns:
   {"byHour":[],"outcomes":[],"signals":[]}
   But when I started a temporary gateway with server/api_gateway/.env loaded, the same endpoint returned BTCUSDT history, outcomes, and hourly rows.
2. Neon actually has data.
   I queried it directly:
   - signals: 393,947 rows
   - BTCUSDT confidence rows: 188,597
   - trade_outcomes: 217,172 rows
   - BTCUSDT near-misses: 77,326, latest entry on 2026-09-16
   - BTCUSDT real signal outcomes: 617, latest entry on 2026-09-14
3. Live signal panels are empty because nothing is crossing threshold right now.
   In an 8-second Redis sample:
   - market.ticks: 2148
   -  0market.scores: 2135
   - market.signals: 0
   - trade.decisions:
   - max live score seen: 50.3
   - threshold: 75.0
   So “latest signal confidence”, “signal feed”, and “risk shield decisions” are empty because Bot 2 is scoring live ticks, but not emitting BUY signals. Bot 3 only wakes up when market.signals fires.
4. The analytics outcome panels are live-session panels, not history panels.
   trade outcomes and missed opportunities on the analytics page come from SSE events in use_signal_stream.ts, not from /history. So after refresh they can say “Nothing resolved yet” even though Neon has old resolved rows.




   I’ll treat this like a system review, not a trading signal. I’m going to inspect the model trainer, analyst scoring, outcome tracker, gateway/UI wiring, then I’ll write a CHANGES.md review with concrete upgrade paths, math/algorithm candidates, and what I’d trust only in paper mode versus live mode.




  Ran commands, searched the web


I’m going to pull the current trainer design and any saved model metadata now. That will let the recommendations be grounded in your actual rows and code, not generic “add AI” fluff.




Read files


A couple of sharp edges are already visible: the trainer queries entry_time but later prints created_at, so it may crash before saving; the Go scanner also appends each closed candle twice, which can distort RSI/volatility. I’ll keep those as recommendations for now and document them clearly instead of changing live behavior.




  Ran commands


The saved model metadata read is taking a little longer than expected, probably importing sklearn from the venv. I’m letting it finish, then I’ll query the training table directly to see the actual label balance and date ranges.




The existing trained_model.pkl is stale/incomplete: it only lists four features and has no saved metrics/periods, while the current trainer expects five features including macd_hist. I’m going to run the trainer against a temporary output filename so I can see whether it trains cleanly without overwriting your current artifact.




  Ran commands


The database stats are useful: nearly all labeled win/loss rows are near-misses, while signal rows currently have wins and timeouts but no losses in that query. That means a naive model can learn the near-miss detector more than the trade signal quality unless we split/filter carefully.




The temp trainer is still running, probably fitting on ~121k labeled rows. I’m giving it more time, then I’ll either capture its metrics or note the bottleneck.




Worked for 4m 7s




Still no metrics yet after roughly two minutes. Since this was just a non-overwriting probe, I’m stopping it and I’ll document “trainer performance/diagnostics” as part of the improvement plan rather than letting it grind blindly.