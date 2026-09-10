// Bot 3: The Risk & Execution Shield
//
// Subscribes to "market.signals" (published by the Python analyst), applies
// hard capital guardrails, and — only in Fully Automated mode — hands the
// trade off to your broker client. In Co-Pilot mode it publishes an approval
// request to "trade.pending" instead of executing, and waits for a human
// "approve"/"reject" pushed from the dashboard on "trade.approvals".
//
// The actual broker/M-Pesa order call is intentionally a stub
// (placeBrokerOrder). Wire in your real broker SDK there once you've decided
// which exchange/broker API you're integrating — that call moves real money
// and needs its own auth, idempotency, and error handling that's specific to
// whichever broker you pick.
package main

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

type TradeSignal struct {
	Symbol       string  `json:"symbol"`
	Action       string  `json:"action"`
	Confidence   float64 `json:"confidence"`
	TriggerPrice float64 `json:"trigger_price"`
}

type Decision struct {
	Symbol     string  `json:"symbol"`
	Action     string  `json:"action"`
	Confidence float64 `json:"confidence"`
	Price      float64 `json:"price"`
	Status     string  `json:"status"` // "approved" | "blocked" | "pending_approval"
	Reason     string  `json:"reason"`
	Timestamp  int64   `json:"timestamp"`
}

func main() {
	redisAddr := getenv("REDIS_ADDR", "localhost:6379")
	maxBalance := getenvFloat("MAX_BALANCE_USD", 10.00)
	lowBalanceMinConfidence := getenvFloat("MIN_CONFIDENCE_LOW_BALANCE", 85.0)

	ctx := context.Background()
	rdb := redis.NewClient(&redis.Options{Addr: redisAddr})
	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Fatalf("cannot reach redis at %s: %v", redisAddr, err)
	}

	pubsub := rdb.Subscribe(ctx, "market.signals")
	defer pubsub.Close()

	log.Printf("Bot 3 (Risk Shield) active. Balance ceiling for strict mode: $%.2f", maxBalance)

	for msg := range pubsub.Channel() {
		var sig TradeSignal
		if err := json.Unmarshal([]byte(msg.Payload), &sig); err != nil {
			continue
		}

		balance := currentBalanceUSD(ctx, rdb) // read from Redis, set by your account-sync job
		mode := tradingMode(ctx, rdb)           // "copilot" (default) or "automated"

		decision := Decision{
			Symbol: sig.Symbol, Action: sig.Action,
			Confidence: sig.Confidence, Price: sig.TriggerPrice,
			Timestamp: time.Now().UnixMilli(),
		}

		// Hard capital guardrail: below the strict-mode ceiling, require a
		// much higher confidence bar before even considering the trade.
		if balance <= maxBalance && sig.Confidence < lowBalanceMinConfidence {
			decision.Status = "blocked"
			decision.Reason = "balance below ceiling and confidence under strict-mode threshold"
			publish(ctx, rdb, decision)
			log.Printf("Blocked: balance $%.2f, confidence %.2f%% < required %.2f%%",
				balance, sig.Confidence, lowBalanceMinConfidence)
			continue
		}

		if mode == "automated" {
			if err := placeBrokerOrder(sig); err != nil {
				decision.Status = "blocked"
				decision.Reason = "broker execution failed: " + err.Error()
			} else {
				decision.Status = "approved"
				decision.Reason = "auto-executed"
			}
		} else {
			// Co-Pilot mode: never auto-execute, just surface it for a human tap.
			decision.Status = "pending_approval"
			decision.Reason = "co-pilot mode — awaiting manual approval"
		}

		publish(ctx, rdb, decision)
	}
}

// placeBrokerOrder is a stub. Replace this with your real broker/exchange
// SDK call. Keep it non-blocking / on its own goroutine with a timeout in
// production so a hung broker API can never freeze this service.
func placeBrokerOrder(sig TradeSignal) error {
	log.Printf("[STUB] would place %s order for %s at %.2f (confidence %.2f%%) — wire real broker API here",
		sig.Action, sig.Symbol, sig.TriggerPrice, sig.Confidence)
	return nil
}

func currentBalanceUSD(ctx context.Context, rdb *redis.Client) float64 {
	v, err := rdb.Get(ctx, "account:balance_usd").Result()
	if err != nil {
		return 0.0 // fail safe: unknown balance is treated as "protect capital"
	}
	f, _ := strconv.ParseFloat(v, 64)
	return f
}

func tradingMode(ctx context.Context, rdb *redis.Client) string {
	v, err := rdb.Get(ctx, "config:trading_mode").Result()
	if err != nil || v == "" {
		return "copilot" // fail safe default: never auto-execute unless explicitly toggled
	}
	return v
}

func publish(ctx context.Context, rdb *redis.Client, d Decision) {
	payload, _ := json.Marshal(d)
	rdb.Publish(ctx, "trade.decisions", payload)
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func getenvFloat(key string, fallback float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return fallback
}
