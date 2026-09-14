// Bot 1: The Scanner
//
// Connects to a real public market data feed (Binance combined trade stream),
// maintains a rolling window per symbol, and computes lightweight technical
// indicators (RSI-14, rolling volatility) on every tick. Publishes an enriched
// tick to Redis channel "market.ticks" for Bot 2 (Python analyst) to consume.
//
// This service does NOT place trades and does NOT touch your balance. It only
// reads public data and does math.
package main

import (
	"context"
	"encoding/json"
	"log"
	"math"
	"net/url"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
)

// Symbols you want to scan. Add/remove freely — each symbol gets its own
var symbols = []string{"btcusdt", "ethusdt", "solusdt", "bnbusdt", "xrpusdt", "dogeusdt"}

const rsiPeriod = 14
const windowSize = 60 // number of recent prices kept for volatility calc

type EnrichedTick struct {
	Symbol       string  `json:"symbol"`
	Price        float64 `json:"price"`
	RSI14        float64 `json:"rsi_14"`
	Volatility   float64 `json:"volatility"` // stddev of last N log returns
	Timestamp    int64   `json:"timestamp"`
}

// rollingSeries keeps enough history per symbol to compute RSI + volatility
// without ever growing unbounded (fixed-size ring buffer behaviour via slicing).
type rollingSeries struct {
	mu     sync.Mutex
	prices []float64
}

func (r *rollingSeries) push(price float64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.prices = append(r.prices, price)
	if len(r.prices) > windowSize {
		r.prices = r.prices[len(r.prices)-windowSize:]
	}
}

func (r *rollingSeries) rsi14() float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.prices) < rsiPeriod+1 {
		return 50.0 // neutral until we have enough data
	}
	var gains, losses float64
	start := len(r.prices) - rsiPeriod - 1
	for i := start + 1; i < len(r.prices); i++ {
		delta := r.prices[i] - r.prices[i-1]
		if delta >= 0 {
			gains += delta
		} else {
			losses -= delta
		}
	}
	if losses == 0 {
		return 100.0
	}
	rs := (gains / rsiPeriod) / (losses / rsiPeriod)
	return 100.0 - (100.0 / (1.0 + rs))
}

func (r *rollingSeries) volatility() float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.prices) < 3 {
		return 0.0
	}
	returns := make([]float64, 0, len(r.prices)-1)
	for i := 1; i < len(r.prices); i++ {
		if r.prices[i-1] == 0 {
			continue
		}
		returns = append(returns, math.Log(r.prices[i]/r.prices[i-1]))
	}
	var mean float64
	for _, v := range returns {
		mean += v
	}
	mean /= float64(len(returns))
	var variance float64
	for _, v := range returns {
		variance += (v - mean) * (v - mean)
	}
	variance /= float64(len(returns))
	return math.Sqrt(variance)
}

type binanceTradeMsg struct {
	Stream string `json:"stream"`
	Data   struct {
		Symbol string `json:"s"`
		Price  string `json:"p"`
	} `json:"data"`
}

func main() {
	redisAddr := getenv("REDIS_ADDR", "localhost:6379")
	rdb := redis.NewClient(&redis.Options{Addr: redisAddr})
	ctx := context.Background()

	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Fatalf("cannot reach redis at %s: %v", redisAddr, err)
	}

	series := make(map[string]*rollingSeries)
	for _, s := range symbols {
		series[s] = &rollingSeries{}
	}

	streamParam := ""
	for i, s := range symbols {
		if i > 0 {
			streamParam += "/"
		}
		streamParam += s + "@trade"
	}

	u := url.URL{
		Scheme:   "wss",
		Host:     "stream.binance.com:9443",
		Path:     "/stream",
		RawQuery: "streams=" + streamParam,
	}

	log.Printf("Smith (Scanner) connecting to %s", u.String())

	for {
		if err := runOnce(ctx, u.String(), rdb, series); err != nil {
			log.Printf("scanner disconnected: %v — reconnecting in 3s", err)
			time.Sleep(3 * time.Second)
		}
	}
}

func runOnce(ctx context.Context, wsURL string, rdb *redis.Client, series map[string]*rollingSeries) error {
	conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		return err
	}
	defer conn.Close()

	log.Println("Smith (Scanner) connected. Streaming live trades...")

	for {
		_, msg, err := conn.ReadMessage()
		if err != nil {
			return err
		}

		var trade binanceTradeMsg
		if err := json.Unmarshal(msg, &trade); err != nil {
			continue
		}
		if trade.Data.Symbol == "" {
			continue
		}

		price, err := strconv.ParseFloat(trade.Data.Price, 64)
		if err != nil {
			continue
		}

		symKey := trade.Data.Symbol // e.g. "BTCUSDT"
		rs, ok := series[symKeyLower(symKey, series)]
		if !ok {
			continue
		}
		rs.push(price)

		tick := EnrichedTick{
			Symbol:     symKey,
			Price:      price,
			RSI14:      rs.rsi14(),
			Volatility: rs.volatility(),
			Timestamp:  time.Now().UnixMilli(),
		}

		payload, _ := json.Marshal(tick)
		if err := rdb.Publish(ctx, "market.ticks", payload).Err(); err != nil {
			log.Printf("redis publish failed: %v", err)
		}
	}
}

// symKeyLower matches Binance's UPPERCASE stream symbol back to our lowercase
// config keys so we hit the right rolling window.
func symKeyLower(binanceSymbol string, series map[string]*rollingSeries) string {
	for k := range series {
		if len(k) == len(binanceSymbol) {
			match := true
			for i := 0; i < len(k); i++ {
				c := binanceSymbol[i]
				if c >= 'A' && c <= 'Z' {
					c += 'a' - 'A'
				}
				if c != k[i] {
					match = false
					break
				}
			}
			if match {
				return k
			}
		}
	}
	return ""
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
