// Bot 1: The Scanner
//
// Connects to a real public market data feed (Binance combined trade stream)
// and builds fixed-interval candles (default 1 minute) per symbol from the
// raw trade stream. RSI-14, volatility, and MACD are computed over CLOSED
// CANDLES, not raw trade prints. A longer rolling window also feeds
// trend_bias — price's position relative to a multi-hour moving average —
// giving the analyst a regime signal the short-window indicators can't see
// on their own (added after cross-validated training showed the model's
// predictive direction flipping across days with different market trends).
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
const candleWindowSize = 60  // short window for RSI/vol/MACD
const longWindowSize = 240   // long window for trend_bias (240 x 1-min = 4 hours)

type EnrichedTick struct {
	Symbol     string  `json:"symbol"`
	Price      float64 `json:"price"`
	RSI14      float64 `json:"rsi_14"`
	Volatility float64 `json:"volatility"`
	MACDHist   float64 `json:"macd_hist"`
	TrendBias  float64 `json:"trend_bias"`
	Timestamp  int64   `json:"timestamp"`
}

// candleAggregator builds fixed-interval candles from raw trade prices and
// keeps rolling windows of closed candle closes, from which RSI/volatility/
// MACD/trend_bias are computed. All are cached and only recomputed when a
// candle actually closes — not on every raw trade.
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
	longCloses  []float64

	cachedRSI  float64
	cachedVol  float64
	cachedTrend float64

	ema12        float64
	ema26        float64
	macdSignal   float64
	macdReady    bool
	cachedMACD   float64
	cachedSignal float64
	cachedHist   float64
}

func newCandleAggregator(interval time.Duration) *candleAggregator {
	return &candleAggregator{interval: interval, cachedRSI: 50.0, cachedVol: 0.0, cachedTrend: 0.0}
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
		// Current candle is done — record its close ONCE, recompute indicators.
		c.closes = append(c.closes, c.close)
		if len(c.closes) > candleWindowSize {
			c.closes = c.closes[len(c.closes)-candleWindowSize:]
		}

		c.longCloses = append(c.longCloses, c.close)
		if len(c.longCloses) > longWindowSize {
			c.longCloses = c.longCloses[len(c.longCloses)-longWindowSize:]
		}

		c.cachedRSI = computeRSI(c.closes)
		c.cachedVol = computeVolatility(c.closes)
		c.cachedTrend = computeTrendBias(c.longCloses, c.close)
		c.updateMACD(c.close)

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

func (c *candleAggregator) snapshot() (rsi, vol, macdHist, trendBias float64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.cachedRSI, c.cachedVol, c.cachedHist, c.cachedTrend
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

// computeTrendBias returns price's position relative to its own longer-term
// moving average: positive means price is above the long average (uptrend
// bias), negative means below (downtrend bias). Needs at least 30 closed
// candles before it returns anything other than neutral (0.0), since a
// trend read off too little history is just noise.
func computeTrendBias(longCloses []float64, currentPrice float64) float64 {
	if len(longCloses) < 30 {
		return 0.0
	}
	var sum float64
	for _, v := range longCloses {
		sum += v
	}
	longMA := sum / float64(len(longCloses))
	if longMA == 0 {
		return 0.0
	}
	return (currentPrice - longMA) / longMA
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
		rsi, vol, macdHist, trendBias := agg.snapshot()

		tick := EnrichedTick{
			Symbol:     symKey,
			Price:      price,
			RSI14:      rsi,
			Volatility: vol,
			MACDHist:   macdHist,
			TrendBias:  trendBias,
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