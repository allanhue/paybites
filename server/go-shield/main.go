// Bot 3: The Risk & Execution Shield
//
// Subscribes to "market.signals", applies capital guardrails, and — only in
// Fully Automated mode — hands approved trades to the execution-bridge
// (Python) via "trade.execute". Co-Pilot mode never executes; it only
// surfaces a pending_approval decision for a human to review.
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
	Status     string  `json:"status"`
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

	log.Printf("Horro (Risk Shield) active. Balance ceiling for strict mode: $%.2f", maxBalance)

	for msg := range pubsub.Channel() {
		var sig TradeSignal
		if err := json.Unmarshal([]byte(msg.Payload), &sig); err != nil {
			continue
		}

		balance := currentBalanceUSD(ctx, rdb)
		mode := tradingMode(ctx, rdb)

		decision := Decision{
			Symbol: sig.Symbol, Action: sig.Action,
			Confidence: sig.Confidence, Price: sig.TriggerPrice,
			Timestamp: time.Now().UnixMilli(),
		}

		if balance <= maxBalance && sig.Confidence < lowBalanceMinConfidence {
			decision.Status = "blocked"
			decision.Reason = "balance below ceiling and confidence under strict-mode threshold"
			publish(ctx, rdb, decision)
			log.Printf("Blocked: balance $%.2f, confidence %.2f%% < required %.2f%%",
				balance, sig.Confidence, lowBalanceMinConfidence)
			continue
		}

		if mode == "automated" {
			payload, _ := json.Marshal(sig)
			if err := rdb.Publish(ctx, "trade.execute", payload).Err(); err != nil {
				decision.Status = "blocked"
				decision.Reason = "failed to hand off to execution bridge: " + err.Error()
			} else {
				decision.Status = "approved"
				decision.Reason = "dispatched to execution bridge"
			}
		} else {
			decision.Status = "pending_approval"
			decision.Reason = "co-pilot mode — awaiting manual approval"
		}

		publish(ctx, rdb, decision)
	}
}

func currentBalanceUSD(ctx context.Context, rdb *redis.Client) float64 {
	v, err := rdb.Get(ctx, "account:balance_usd").Result()
	if err != nil {
		return 0.0
	}
	f, _ := strconv.ParseFloat(v, 64)
	return f
}

func tradingMode(ctx context.Context, rdb *redis.Client) string {
	v, err := rdb.Get(ctx, "config:trading_mode").Result()
	if err != nil || v == "" {
		return "copilot"
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