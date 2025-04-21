import asyncio
import json
import os
import re
import sys

import requests
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
HTML_SITEMAP_URL = "https://thebearcave.substack.com/sitemap/2024"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/bearcave_html_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("BEARCAVE_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BEARCAVE_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_processed_urls():
    try:
        with open(PROCESSED_URLS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_urls(urls):
    with open(PROCESSED_URLS_FILE, "w") as f:
        json.dump(list(urls), f, indent=2)
    log_message("Processed URLs saved.", "INFO")


def fetch_html_sitemap():
    try:
        response = requests.get(HTML_SITEMAP_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        posts = []
        for link in soup.select(
            "#main > div.container.typography.sitemap-page > div > p > a"
        ):
            posts.append({"title": link.text.strip(), "canonical_url": link["href"]})

        log_message(f"Fetched {len(posts)} posts from HTML sitemap", "INFO")
        return posts
    except Exception as e:
        log_message(f"Error fetching HTML sitemap: {e}", "ERROR")
        return []


def extract_ticker(title):
    if title is not None and title.startswith("Problems at"):
        # Look for text within parentheses
        match = re.search(r"\((.*?)\)", title)
        if match:
            potential_ticker = match.group(1)
            # Verify it's all uppercase
            if potential_ticker.isupper():
                return potential_ticker
    return None


async def send_to_telegram(post_data, ticker=None):
    current_time = get_current_time()

    message = f"<b>New Bear Cave Article - HTML!</b>\n\n"
    message += f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Title:</b> {post_data['title']}\n"
    message += f"<b>URL:</b> {post_data['canonical_url']}\n"

    if ticker:
        message += f"<b>Ticker:</b> {ticker}\n"
        await send_ws_message(
            {
                "name": "The Bear Cave - H",
                "type": "Sell",
                "ticker": ticker,
                "sender": "bearcave",
            },
        )
        log_message(
            f"Ticker sent to WebSocket: {ticker} - {post_data['canonical_url']}", "INFO"
        )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Article sent to Telegram: {post_data['canonical_url']}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()

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
            posts = fetch_html_sitemap()

            new_posts = [
                post
                for post in posts
                if post.get("canonical_url")
                and post["canonical_url"] not in processed_urls
            ]

            if new_posts:
                log_message(f"Found {len(new_posts)} new posts to process.", "INFO")

                for post in new_posts:
                    if not "title" in post:
                        continue

                    ticker = extract_ticker(post["title"])
                    await send_to_telegram(post, ticker)
                    processed_urls.add(post["canonical_url"])

                save_processed_urls(processed_urls)
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
