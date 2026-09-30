// Bot 1: The Scanner
//
// Connects to a real public market data feed (Binance combined trade stream)
// and builds fixed-interval candles (default 1 minute) per symbol from the
// raw trade stream. RSI-14, volatility, and MACD are computed over CLOSED
// CANDLES, not raw trade prints. A longer rolling window also feeds
// trend_bias — price's position relative to a multi-hour moving average —
// giving the analyst a regime signal the short-window indicators can't see
// on their own.
//
// v2: trend_bias is computed against the LIVE price on every tick (it used to
// be frozen at the last candle close), the window is 2 hours, and it needs 30
// closed candles before reporting anything other than 0.0.
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
const candleWindowSize = 60 // short window for RSI/vol/MACD
const longWindowSize = 120  // long window for trend_bias (120 x 1-min = 2 hours)
const minTrendCandles = 30  // no trend reading until this many closed candles exist
const momentumLookback = 10 // momentum = live price vs the close 10 candles (10 min) ago
const bandWindow = 20       // Bollinger-style band over the last 20 closed candles

type EnrichedTick struct {
	Symbol     string  `json:"symbol"`
	Price      float64 `json:"price"`
	RSI14      float64 `json:"rsi_14"`
	Volatility float64 `json:"volatility"`
	MACDHist   float64 `json:"macd_hist"`
	TrendBias  float64 `json:"trend_bias"`
	Momentum     float64 `json:"momentum"`
	BandPosition float64 `json:"band_position"`
	Timestamp  int64   `json:"timestamp"`
}

// candleAggregator builds fixed-interval candles from raw trade prices and
// keeps rolling windows of closed candle closes, from which RSI/volatility/
// MACD are computed (cached, recomputed only when a candle closes). trend_bias
// is derived from the long window plus the live price at snapshot time.
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

	cachedRSI float64
	cachedVol float64

	ema12        float64
	ema26        float64
	macdSignal   float64
	macdReady    bool
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

type snapshotData struct {
	RSI, Vol, MACDHist, TrendBias, Momentum, BandPos float64
}

// snapshot returns the cached indicators plus trend_bias, momentum and band
// position measured against the live price passed in. Momentum and band
// position are built from CLOSED CANDLES (the old analyst versions used the
// last 10/20 raw trade prints, i.e. a fraction of a second on busy symbols).
func (c *candleAggregator) snapshot(price float64) snapshotData {
	c.mu.Lock()
	defer c.mu.Unlock()
	return snapshotData{
		RSI:       c.cachedRSI,
		Vol:       c.cachedVol,
		MACDHist:  c.cachedHist,
		TrendBias: computeTrendBias(c.longCloses, price),
		Momentum:  computeCandleMomentum(c.closes, price),
		BandPos:   computeBandPosition(c.closes, price),
	}
}

// computeCandleMomentum: fractional change of the live price vs the close
// momentumLookback candles ago. 0.0 until enough candles exist.
func computeCandleMomentum(closes []float64, price float64) float64 {
	if len(closes) < momentumLookback {
		return 0.0
	}
	base := closes[len(closes)-momentumLookback]
	if base == 0 {
		return 0.0
	}
	return (price - base) / base
}

// computeBandPosition: where the live price sits inside a 2-sigma band of the
// last bandWindow closes. -1 = lower band, 0 = mean, +1 = upper band, clipped
// to [-1.5, 1.5]. Same formula the analyst used, now on candles.
func computeBandPosition(closes []float64, price float64) float64 {
	if len(closes) < bandWindow {
		return 0.0
	}
	w := closes[len(closes)-bandWindow:]
	var sum float64
	for _, v := range w {
		sum += v
	}
	mean := sum / float64(len(w))
	var variance float64
	for _, v := range w {
		variance += (v - mean) * (v - mean)
	}
	sd := math.Sqrt(variance / float64(len(w)))
	if sd < 1e-12 {
		return 0.0
	}
	return math.Max(-1.5, math.Min(1.5, (price-mean)/(2*sd)))
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
// bias), negative means below (downtrend bias). Returns exactly 0.0 until
// minTrendCandles closed candles exist, since a trend read off too little
// history is just noise. (The trainer drops rows where trend_bias == 0.0.)
func computeTrendBias(longCloses []float64, currentPrice float64) float64 {
	if len(longCloses) < minTrendCandles {
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
		s := agg.snapshot(price)

		tick := EnrichedTick{
			Symbol:       symKey,
			Price:        price,
			RSI14:        s.RSI,
			Volatility:   s.Vol,
			MACDHist:     s.MACDHist,
			TrendBias:    s.TrendBias,
			Momentum:     s.Momentum,
			BandPosition: s.BandPos,
			Timestamp:    now.UnixMilli(),
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