import asyncio
import json
import os
import re
import sys
from typing import Optional

import aiohttp
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

# Constants
API_URL = "https://morpheus-research.ghost.io/ghost/api/content/posts/"
API_KEY = os.getenv("MORPHEUS_API_KEY")
CHECK_INTERVAL = 0.3  # seconds
PROCESSED_POSTS_FILE = "data/morpheus_processed_posts.json"
TELEGRAM_BOT_TOKEN = os.getenv("MORPHEUS_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("MORPHEUS_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


class TickerInfo:
    def __init__(self, ticker: str, exchange: str):
        self.ticker = ticker
        self.exchange = exchange


def load_processed_posts():
    try:
        with open(PROCESSED_POSTS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_posts(post_ids):
    with open(PROCESSED_POSTS_FILE, "w") as f:
        json.dump(list(post_ids), f, indent=2)
    log_message("Processed post IDs saved.", "INFO")


def extract_ticker_from_html(html_content: str) -> Optional[TickerInfo]:
    soup = BeautifulSoup(html_content, "html.parser")
    text_content = soup.get_text()

    # Single pattern to match both NYSE and Nasdaq with case insensitivity
    pattern = r"\(\s*((?:NYSE|NASDAQ))(?:\s*:\s*)([A-Z]+)\s*\)"

    # Case insensitive search
    match = re.search(pattern, text_content, re.IGNORECASE)
    if match:
        exchange = match.group(1).upper()  # Normalize to uppercase
        # Convert to standard form (NASDAQ instead of Nasdaq)
        if exchange.upper() == "NASDAQ":
            exchange = "NASDAQ"
        else:
            exchange = "NYSE"
        return TickerInfo(ticker=match.group(2), exchange=exchange)

    return None


async def fetch_posts(session):
    params = {
        "key": API_KEY,
        "limit": 1,
        "filter": "status:draft",
        "fields": "title,url,excerpt,created_at",
    }

    try:
        async with session.get(API_URL, params=params) as response:
            if response.status == 200:
                data = await response.json()
                posts = data.get("posts", [])
                log_message(f"Fetched {len(posts)} posts from API", "INFO")
                return posts
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return []
            else:
                log_message(f"Failed to fetch posts: HTTP {response.status}", "ERROR")
                return []
    except Exception as e:
        log_message(f"Error fetching posts: {e}", "ERROR")
        return []


async def send_to_telegram(post, ticker_info: Optional[TickerInfo]):
    current_time = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
    created_time = post.get("created_at", "Unknown")
    post_url = post.get("url", "")
    title = post.get("title", "Untitled Post")

    message = f"<b>New Morpheus Research Report</b>\n\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Current Time:</b> {current_time}\n"
    message += f"<b>Post Created:</b> {created_time}\n"
    message += f"<b>URL:</b> {post_url}\n"

    if ticker_info:
        message += f"\n<b>Ticker:</b> {ticker_info.ticker}\n"
        message += f"<b>Exchange:</b> {ticker_info.exchange}\n"

        await send_ws_message(
            {
                "name": "Morpheus Research",
                "type": "Sell",
                "ticker": ticker_info.ticker,
                "sender": "morpheus",
                "target": "CSS",
            },
        )
        log_message(
            f"Report sent to Telegram and WebSocket for: {ticker_info.ticker} - {post_url}",
            "INFO",
        )
    else:
        log_message(f"Report sent to Telegram (no ticker found) - {post_url}", "INFO")

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def run_scraper():
    processed_post_ids = load_processed_posts()

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
                    post
                    for post in posts
                    if post.get("id") and post["id"] not in processed_post_ids
                ]

                if new_posts:
                    log_message(f"Found {len(new_posts)} new posts to process.", "INFO")

                    for post in new_posts:
                        ticker_info = extract_ticker_from_html(post.get("excerpt", ""))
                        # FIX: Make sure to handle this in the future if no page reutrns
                        # if not ticker_info:
                        #     ticker_info = extract_ticker_from_html(post.get("html", ""))
                        #
                        await send_to_telegram(post, ticker_info)
                        processed_post_ids.add(post["id"])

                    save_processed_posts(processed_post_ids)
                else:
                    log_message("No new posts found.", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
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
