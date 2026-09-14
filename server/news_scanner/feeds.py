"""
Public RSS feeds — headlines and links only, never full article text.
Add/remove sources here.
"""

FEEDS = {
    "crypto": [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
    ],
    "forex": [
        "https://www.forexlive.com/feed/news",
        "https://www.investing.com/rss/news_301.rss",
    ],
}

# Rough keyword → symbol tagging, so a headline can be linked to what it's about
SYMBOL_KEYWORDS = {
    "BTCUSDT": ["bitcoin", "btc"],
    "ETHUSDT": ["ethereum", "eth"],
    "XAUUSDc": ["gold", "xau"],
    "EURUSDc": ["euro", "ecb", "eurozone"],
    "GBPUSDc": ["pound", "sterling", "boe"],
    "USDJPYc": ["yen", "boj", "japan"],
}