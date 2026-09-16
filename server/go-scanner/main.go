// Bot 1: The Scanner
//
// Connects to a real public market data feed (Binance combined trade stream)
// and builds fixed-interval candles (default 1 minute) per symbol from the
// raw trade stream. RSI-14 and volatility are computed over the last 14
// CLOSED CANDLES, not raw trade prints — this matches how RSI is meant to
// work everywhere in finance. Computing RSI over 14 raw WebSocket trades
// (the previous approach) meant the window could span anywhere from a few
// hundred milliseconds to several seconds depending on trade frequency,
// causing RSI to saturate at 0 or 100 constantly on busy symbols (BTC/ETH)
// as an artifact of tick-count windowing, not genuine oversold/overbought
// conditions. This version fixes that.
//
// Every individual trade still publishes an immediate price update to
// "market.ticks" (so downstream services stay real-time on price), but the
// rsi_14/volatility fields only change when a candle actually closes.
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

var symbols = []string{"btcusdt", "ethusdt", "solusdt", "bnbusdt", "xrpusdt", "dogeusdt"}

const rsiPeriod = 14
const candleWindowSize = 60 // how many closed candles to retain per symbol

type EnrichedTick struct {
	Symbol     string  `json:"symbol"`
	Price      float64 `json:"price"`
	RSI14      float64 `json:"rsi_14"`
	Volatility float64 `json:"volatility"`
	MACDHist   float64 `json:"macd_hist"`
	Timestamp  int64   `json:"timestamp"`
}

// candleAggregator builds fixed-interval candles from raw trade prices and
// keeps a rolling window of closed candle closes, from which RSI/volatility
// are computed. RSI/volatility are cached and only recomputed when a candle
// actually closes — not on every raw trade.
type candleAggregator struct {
	mu          sync.Mutex
	interval    time.Duration
	candleStart time.Time
	open        float64
	high        float64
	low         float64
	close       float64
	hasCandle   bool
	closes      []float64

	cachedRSI float64
	cachedVol float64

	// MACD state — EMAs updated only on candle close, same discipline as RSI/vol
	ema12        float64
	ema26        float64
	macdSignal   float64
	macdReady    bool // false until ema12/ema26 have seen enough candles to mean something
	cachedMACD   float64
	cachedSignal float64
	cachedHist   float64
}

func newCandleAggregator(interval time.Duration) *candleAggregator {
	return &candleAggregator{interval: interval, cachedRSI: 50.0, cachedVol: 0.0}
}

// pushTrade updates the in-progress candle with a new trade price, closing
// and rolling over to a new candle if the trade crosses into a new interval.
func (c *candleAggregator) pushTrade(price float64, ts time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()

	bucketStart := ts.Truncate(c.interval)

	if !c.hasCandle {
		c.candleStart = bucketStart
		c.open, c.high, c.low, c.close = price, price, price, price
		c.hasCandle = true
		return
	}

	if bucketStart.After(c.candleStart) {
		// Current candle is done — record its close, recompute indicators.
		c.closes = append(c.closes, c.close)
		if len(c.closes) > candleWindowSize {
			c.closes = c.closes[len(c.closes)-candleWindowSize:]
		}
		 c.closes = append(c.closes, c.close)
		if len(c.closes) > candleWindowSize {
			c.closes = c.closes[len(c.closes)-candleWindowSize:]
		}
		c.cachedRSI = computeRSI(c.closes)
		c.cachedVol = computeVolatility(c.closes)
		c.updateMACD(c.close) // NEW

		// Start the new candle.
		c.candleStart = bucketStart
		c.open, c.high, c.low, c.close = price, price, price, price
		return
	}

	// Still inside the current candle — update close (and high/low, unused
	// downstream today but kept for future features like ATR).
	c.close = price
	if price > c.high {
		c.high = price
	}
	if price < c.low {
		c.low = price
	}
}

func (c *candleAggregator) snapshot() (rsi, vol, macdHist float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.cachedRSI, c.cachedVol, c.cachedHist
	
}


func computeRSI(closes []float64) float64 {
	if len(closes) < rsiPeriod+1 {
		return 50.0 // neutral until we have enough closed candles
	}
	var gains, losses float64
	start := len(closes) - rsiPeriod - 1
	for i := start + 1; i < len(closes); i++ {
		delta := closes[i] - closes[i-1]
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

func computeVolatility(closes []float64) float64 {
	if len(closes) < 3 {
		return 0.0
	}
	returns := make([]float64, 0, len(closes)-1)
	for i := 1; i < len(closes); i++ {
		if closes[i-1] == 0 {
			continue
		}
		returns = append(returns, math.Log(closes[i]/closes[i-1]))
	}
	if len(returns) == 0 {
		return 0.0
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
	candleSeconds := getenvInt("CANDLE_SECONDS", 60)
	rdb := redis.NewClient(&redis.Options{Addr: redisAddr})
	ctx := context.Background()

	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Fatalf("cannot reach redis at %s: %v", redisAddr, err)
	}

	interval := time.Duration(candleSeconds) * time.Second
	aggregators := make(map[string]*candleAggregator)
	for _, s := range symbols {
		aggregators[s] = newCandleAggregator(interval)
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

	log.Printf("Smith (Scanner) connecting to %s (candle interval: %s)", u.String(), interval)

	for {
		if err := runOnce(ctx, u.String(), rdb, aggregators); err != nil {
			log.Printf("scanner disconnected: %v — reconnecting in 3s", err)
			time.Sleep(3 * time.Second)
		}
	}
}

func runOnce(ctx context.Context, wsURL string, rdb *redis.Client, aggregators map[string]*candleAggregator) error {
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

		symKey := trade.Data.Symbol
		agg, ok := aggregators[symKeyLower(symKey, aggregators)]
		if !ok {
			continue
		}

		now := time.Now()
		agg.pushTrade(price, now)
		rsi, vol, macdHist := agg.snapshot()

		tick := EnrichedTick{
			Symbol:     symKey,
			Price:      price,
			RSI14:      rsi,
			Volatility: vol,
			MACDHist:   macdHist,
			Timestamp:  now.UnixMilli(),
		}

		payload, _ := json.Marshal(tick)
		if err := rdb.Publish(ctx, "market.ticks", payload).Err(); err != nil {
			log.Printf("redis publish failed: %v", err)
		}
	}
}

func symKeyLower(binanceSymbol string, aggregators map[string]*candleAggregator) string {
	for k := range aggregators {
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

func getenvInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return fallback
}


func (c *candleAggregator) updateMACD(closePrice float64) {
	const k12 = 2.0 / (12.0 + 1.0)
	const k26 = 2.0 / (26.0 + 1.0)
	const k9 = 2.0 / (9.0 + 1.0)

	if !c.macdReady {
		c.ema12 = closePrice
		c.ema26 = closePrice
		c.macdSignal = 0
		c.macdReady = true
		return
	}

	c.ema12 = (closePrice * k12) + (c.ema12 * (1 - k12))
	c.ema26 = (closePrice * k26) + (c.ema26 * (1 - k26))
	macdLine := c.ema12 - c.ema26

	if c.macdSignal == 0 {
		c.macdSignal = macdLine
	} else {
		c.macdSignal = (macdLine * k9) + (c.macdSignal * (1 - k9))
	}

	c.cachedMACD = macdLine
	c.cachedSignal = c.macdSignal
	c.cachedHist = macdLine - c.macdSignal
}