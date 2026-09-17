// API Gateway
//
// Owns all backend logic the frontend previously did itself: streaming
// Redis pub/sub as SSE, the trading-mode toggle, and Neon history queries.
// The Next.js frontend talks to this over plain HTTP — it holds no business
// logic, no Redis client, no Postgres client of its own anymore.
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/joho/godotenv"
	_ "github.com/lib/pq"
	"github.com/redis/go-redis/v9"
)

var (
	rdb *redis.Client
	db  *sql.DB
	ctx = context.Background()
)

const newsCacheKey = "news:latest"

func main() {
	if err := godotenv.Load(); err != nil {
		log.Println("no .env file found, relying on real environment variables")
	}
	// startHealthServer() // if you added the earlier health-check line, keep it here too

	redisAddr := getenv("REDIS_ADDR", "localhost:6379")
	rdb = redis.NewClient(&redis.Options{Addr: redisAddr})

	dsn := os.Getenv("DATABASE_URL")
	var err error
	db, err = sql.Open("postgres", dsn)
	if err != nil {
		log.Printf("warning: could not open Neon connection: %v (history endpoint will fail)", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", withCORS(handleHealth))
	mux.HandleFunc("/events", withCORS(handleEvents))
	mux.HandleFunc("/mode", withCORS(handleMode))
	mux.HandleFunc("/history", withCORS(handleHistory))
	mux.HandleFunc("/approve", withCORS(handleApprove))
	mux.HandleFunc("/status", withCORS(handleStatus))

	port := getenv("GATEWAY_PORT", "8090")
	log.Printf("API gateway listening on :%s", port)
	log.Fatal(http.ListenAndServe(":"+port, mux))
}

func withCORS(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", getenv("CORS_ORIGIN", "http://localhost:3000"))
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}
		next(w, r)
	}
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Write([]byte("ok"))
}

func handleEvents(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	replayLatestNews(w, flusher, r)

	sub := rdb.Subscribe(r.Context(),
		"market.signals", "trade.decisions", "market.scores",
		"trade.outcomes", "trade.missed", "market.news",
	)
	defer sub.Close()

	ch := sub.Channel()
	heartbeat := time.NewTicker(20 * time.Second)
	defer heartbeat.Stop()

	for {
		select {
		case msg := <-ch:
			fmt.Fprintf(w, "event: %s\ndata: %s\n\n", msg.Channel, msg.Payload)
			flusher.Flush()
		case <-heartbeat.C:
			fmt.Fprint(w, ": ping\n\n")
			flusher.Flush()
		case <-r.Context().Done():
			return
		}
	}
}

func replayLatestNews(w http.ResponseWriter, flusher http.Flusher, r *http.Request) {
	items, err := rdb.LRange(r.Context(), newsCacheKey, 0, 49).Result()
	if err != nil {
		log.Printf("warning: could not replay cached news: %v", err)
		return
	}

	for i := len(items) - 1; i >= 0; i-- {
		fmt.Fprintf(w, "event: market.news\ndata: %s\n\n", items[i])
	}
	if len(items) > 0 {
		flusher.Flush()
	}
}

func handleMode(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		mode, err := rdb.Get(r.Context(), "config:trading_mode").Result()
		if err != nil || mode == "" {
			mode = "copilot"
		}
		json.NewEncoder(w).Encode(map[string]string{"mode": mode})

	case http.MethodPost:
		var body struct {
			Mode string `json:"mode"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, "invalid body", http.StatusBadRequest)
			return
		}
		if body.Mode != "copilot" && body.Mode != "automated" {
			http.Error(w, "mode must be 'copilot' or 'automated'", http.StatusBadRequest)
			return
		}
		if err := rdb.Set(r.Context(), "config:trading_mode", body.Mode, 0).Err(); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		json.NewEncoder(w).Encode(map[string]string{"mode": body.Mode})

	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

type signalRow struct {
	Symbol     string  `json:"symbol"`
	Price      float64 `json:"price"`
	Confidence float64 `json:"confidence"`
	Action     string  `json:"action"`
	CreatedAt  string  `json:"created_at"`
}

type outcomeRow struct {
	Symbol     string   `json:"symbol"`
	Kind       string   `json:"kind"`
	EntryPrice float64  `json:"entry_price"`
	ExitPrice  *float64 `json:"exit_price"`
	Outcome    *string  `json:"outcome"`
	PctChange  *float64 `json:"pct_change"`
	Confidence float64  `json:"confidence"`
	EntryTime  string   `json:"entry_time"`
}

type hourRow struct {
	Hour  float64 `json:"hour"`
	Wins  int     `json:"wins"`
	Total int     `json:"total"`
}

func handleHistory(w http.ResponseWriter, r *http.Request) {
	if db == nil {
		http.Error(w, "database not configured", http.StatusServiceUnavailable)
		return
	}
	symbol := r.URL.Query().Get("symbol")
	if symbol == "" {
		symbol = "BTCUSDT"
	}
	limit := r.URL.Query().Get("limit")
	if limit == "" {
		limit = "200"
	}

	signals := []signalRow{}
	rows, err := db.Query(
		`SELECT symbol, price, confidence, action, created_at
		 FROM signals WHERE symbol = $1 ORDER BY created_at DESC LIMIT $2`,
		symbol, limit,
	)
	if err == nil {
		defer rows.Close()
		for rows.Next() {
			var s signalRow
			rows.Scan(&s.Symbol, &s.Price, &s.Confidence, &s.Action, &s.CreatedAt)
			signals = append(signals, s)
		}
	}

	outcomes := []outcomeRow{}
	rows2, err := db.Query(
		`SELECT symbol, kind, entry_price, exit_price, outcome, pct_change, confidence, entry_time
		 FROM trade_outcomes WHERE symbol = $1 ORDER BY entry_time DESC LIMIT $2`,
		symbol, limit,
	)
	if err == nil {
		defer rows2.Close()
		for rows2.Next() {
			var o outcomeRow
			rows2.Scan(&o.Symbol, &o.Kind, &o.EntryPrice, &o.ExitPrice, &o.Outcome, &o.PctChange, &o.Confidence, &o.EntryTime)
			outcomes = append(outcomes, o)
		}
	}

	byHour := []hourRow{}
	rows3, err := db.Query(
		`SELECT EXTRACT(HOUR FROM entry_time) AS hour,
		        COUNT(*) FILTER (WHERE outcome = 'win') AS wins,
		        COUNT(*) FILTER (WHERE outcome IN ('win','loss')) AS total
		 FROM trade_outcomes WHERE symbol = $1 AND kind = 'signal'
		 GROUP BY hour ORDER BY hour`,
		symbol,
	)
	if err == nil {
		defer rows3.Close()
		for rows3.Next() {
			var h hourRow
			rows3.Scan(&h.Hour, &h.Wins, &h.Total)
			byHour = append(byHour, h)
		}
	}

	json.NewEncoder(w).Encode(map[string]interface{}{
		"signals": signals, "outcomes": outcomes, "byHour": byHour,
	})
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

const approvalTTLSeconds = 60 // matches the frontend countdown

func handleApprove(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var sig struct {
		Symbol       string  `json:"symbol"`
		Confidence   float64 `json:"confidence"`
		TriggerPrice float64 `json:"trigger_price"`
		Timestamp    int64   `json:"timestamp"`
	}
	if err := json.NewDecoder(r.Body).Decode(&sig); err != nil {
		http.Error(w, "invalid body", http.StatusBadRequest)
		return
	}

	ageSeconds := (time.Now().UnixMilli() - sig.Timestamp) / 1000
	if ageSeconds > approvalTTLSeconds {
		http.Error(w, "signal expired — price has likely moved, refresh and wait for a new signal", http.StatusGone)
		return
	}

	payload, _ := json.Marshal(sig)
	if err := rdb.Publish(r.Context(), "trade.execute", payload).Err(); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	json.NewEncoder(w).Encode(map[string]string{"status": "dispatched"})
}

func handleStatus(w http.ResponseWriter, r *http.Request) {
	mode, _ := rdb.Get(r.Context(), "config:trading_mode").Result()
	if mode == "" {
		mode = "copilot"
	}
	threshold := os.Getenv("STRATEGY_THRESHOLD")
	if threshold == "" {
		threshold = "75.0"
	}
	json.NewEncoder(w).Encode(map[string]string{"mode": mode, "threshold": threshold})
}