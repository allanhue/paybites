"""
Bot: The News Scanner

Polls public RSS feeds, scores each headline's sentiment with VADER (a
lightweight, offline, rule-based sentiment tool — no API key, no scraping
full articles, just headline text which RSS explicitly provides for this
purpose), tags it to a symbol by keyword match, and publishes to
"market.news" for the dashboard and (later) the scoring model.
"""

import hashlib
import json
import os
import time

import feedparser
import redis
from dotenv import load_dotenv
import threading
import psycopg
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from feeds import FEEDS, SYMBOL_KEYWORDS

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "")

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
POLL_SECONDS = float(os.getenv("NEWS_POLL_SECONDS", "120"))
NEWS_CACHE_KEY = os.getenv("NEWS_CACHE_KEY", "news:latest")
NEWS_CACHE_LIMIT = int(os.getenv("NEWS_CACHE_LIMIT", "50"))

host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), db=0)
analyzer = SentimentIntensityAnalyzer()

seen_hashes: set[str] = set()
DDL = """
CREATE TABLE IF NOT EXISTS news_log (
    id BIGSERIAL PRIMARY KEY,
    headline TEXT NOT NULL,
    source TEXT,
    sentiment DOUBLE PRECISION,
    symbol TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_news_queue: list[dict] = []
_news_lock = threading.Lock()


def ensure_news_schema():
    if not DATABASE_URL:
        return
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()


def queue_news_row(headline: str, source: str, sentiment: float, symbols: list[str]):
    with _news_lock:
        if symbols:
            for sym in symbols:
                _news_queue.append({"headline": headline, "source": source, "sentiment": sentiment, "symbol": sym})
        else:
            _news_queue.append({"headline": headline, "source": source, "sentiment": sentiment, "symbol": None})


def flush_news():
    if not DATABASE_URL:
        return
    with _news_lock:
        batch, _news_queue[:] = list(_news_queue), []
    if not batch:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO news_log (headline, source, sentiment, symbol) "
                    "VALUES (%(headline)s, %(source)s, %(sentiment)s, %(symbol)s)",
                    batch,
                )
            conn.commit()
    except Exception as e:
        print(f"[news] db flush failed: {e}")

def tag_symbols(headline: str) -> list[str]:
    lower = headline.lower()
    return [sym for sym, keywords in SYMBOL_KEYWORDS.items() if any(k in lower for k in keywords)]


def headline_id(headline: str, link: str) -> str:
    return hashlib.sha256((headline + link).encode()).hexdigest()[:16]


def poll_once():
    for category, urls in FEEDS.items():
        for url in urls:
            try:
                parsed = feedparser.parse(url)
            except Exception as e:
                print(f"[news] failed to fetch {url}: {e}")
                continue

            for entry in parsed.entries[:10]:
                headline = entry.get("title", "")
                link = entry.get("link", "")
                hid = headline_id(headline, link)
                if hid in seen_hashes:
                    continue
                seen_hashes.add(hid)

                sentiment = analyzer.polarity_scores(headline)
                symbols = tag_symbols(headline)

                payload = {
                    "headline": headline,
                    "link": link,
                    "source": category,
                    "sentiment": round(sentiment["compound"], 3),  # -1 (negative) to +1 (positive)
                    "symbols": symbols,
                    "timestamp": int(time.time() * 1000),
                }
                encoded = json.dumps(payload)
                pipe = r.pipeline()
                pipe.lpush(NEWS_CACHE_KEY, encoded)
                pipe.ltrim(NEWS_CACHE_KEY, 0, NEWS_CACHE_LIMIT - 1)
                pipe.publish("market.news", encoded)
                queue_news_row(headline, category, sentiment["compound"], symbols)

                pipe.execute()
                print(f"[news] ({sentiment['compound']:+.2f}) {headline[:80]}")


def main():
    print("News scanner online.")
    while True:
        poll_once()
        # Keep the seen-headlines set from growing forever
        if len(seen_hashes) > 2000:
            seen_hashes.clear()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
