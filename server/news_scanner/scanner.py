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
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from feeds import FEEDS, SYMBOL_KEYWORDS

load_dotenv()

REDIS_ADDR = os.getenv("REDIS_ADDR", "localhost:6379")
POLL_SECONDS = float(os.getenv("NEWS_POLL_SECONDS", "120"))

host, port = REDIS_ADDR.split(":")
r = redis.Redis(host=host, port=int(port), db=0)
analyzer = SentimentIntensityAnalyzer()

seen_hashes: set[str] = set()


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
                r.publish("market.news", json.dumps(payload))
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