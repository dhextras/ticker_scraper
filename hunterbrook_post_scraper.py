import asyncio
import json
import os
import re
import sys
from datetime import datetime

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

JSON_URL = "https://hntrbrk.com/wp-json/wp/v2/posts?per_page=10"
CHECK_INTERVAL = 1
PROCESSED_POSTS_FILE = "data/hunterbrook_processed_posts.json"
TELEGRAM_BOT_TOKEN = os.getenv("HUNTER_BROOK_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("HUNTER_BROOK_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_processed_posts():
    try:
        with open(PROCESSED_POSTS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_posts(posts):
    with open(PROCESSED_POSTS_FILE, "w") as f:
        json.dump(list(posts), f, indent=2)
    log_message("Processed posts saved.", "INFO")


def extract_ticker(text):
    """
    Extract sentiment (buy/sell) and ticker symbol.
    """
    sentiment_match = re.search(
        r"hunterbrook capital is \b(long|short)\b", text, re.IGNORECASE
    )
    if not sentiment_match:
        return None, None

    sentiment_word = sentiment_match.group(1).lower()
    sentiment = "Buy" if sentiment_word == "long" else "Sell"

    # Look for $TICKER or ($TICKER) or (EXCHANGE: $TICKER) patterns after the sentiment position
    text_after_sentiment = text[sentiment_match.end() :]
    ticker_pattern = r"\$([A-Z]+)|\((?:[^:)]*:)?\s*\$?([A-Z]+)\)"
    ticker_match = re.search(ticker_pattern, text_after_sentiment)

    if ticker_match:
        ticker = ticker_match.group(1) or ticker_match.group(2)
        return sentiment, ticker

    return None, None


async def fetch_posts(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with session.get(JSON_URL, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                log_message(f"Fetched {len(data)} posts from JSON", "INFO")
                return data
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return []
            log_message(f"Failed to fetch posts: HTTP {response.status}", "ERROR")
            return []
    except Exception as e:
        log_message(f"Error fetching posts: {e}", "ERROR")
        return []


async def process_post(post):
    title = post["title"]["rendered"]
    content = post["content"]["rendered"]
    link = post["link"]
    post_date = datetime.fromisoformat(post["date"].replace("Z", "+00:00"))
    current_time = get_current_time()

    soup = BeautifulSoup(content, "html.parser")
    box_content = soup.select_one("p.box.sans")
    sentiment, ticker = extract_ticker(box_content.text if box_content else "")

    message = f"<b>New Hunter Brook Research Article</b>\n\n"
    message += f"<b>Post Time:</b> {post_date.astimezone(pytz.timezone('America/Chicago')).strftime('%Y-%m-%d %H:%M:%S')}\n"
    message += f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    message += f"<b>Link:</b> {link}\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Box Content:</b> {box_content.text if box_content else ''}\n"

    if sentiment and ticker:
        message += f"<b>\n\nTicker:</b> {sentiment} - {ticker}\n"

        await send_ws_message(
            {
                "name": "Hunter Brook - Post",
                "type": sentiment,
                "ticker": ticker,
                "sender": "hunterbrook",
                "target": "CSS",
            },
        )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Report sent to Telegram: {ticker} - {link}", "INFO")


async def run_scraper():
    processed_posts = load_processed_posts()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message("Market is open. Starting to check for new posts...", "DEBUG")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking for new posts...")
                posts = await fetch_posts(session)

                new_posts = [
                    post for post in posts if post["link"] not in processed_posts
                ]

                if new_posts:
                    log_message(f"Found {len(new_posts)} new posts to process.", "INFO")
                    for post in new_posts:
                        await process_post(post)
                        processed_posts.add(post["link"])
                    save_processed_posts(processed_posts)
                else:
                    log_message("No new posts found.", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
