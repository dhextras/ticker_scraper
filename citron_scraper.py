import asyncio
import json
import os
import sys

import aiohttp
from dotenv import load_dotenv

from utils.gpt_ticker_extractor import TickerAnalysis, analyze_image_for_ticker
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
JSON_URL = "https://citronresearch.com/wp-json/wp/v2/media"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/citron_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("CITRON_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("CITRON_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

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


async def fetch_json(session):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        async with session.get(JSON_URL, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                log_message(f"Fetched {len(data)} posts from JSON", "INFO")
                return data
            else:
                log_message(f"Failed to fetch JSON: HTTP {response.status}", "ERROR")
                return []
    except Exception as e:
        log_message(f"Error fetching JSON: {e}", "ERROR")
        return []


async def send_to_telegram(url, ticker_object: TickerAnalysis | None):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Citron Research Report</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"

    if ticker_object and ticker_object.found:
        message += f"\n<b>Ticker:</b> {ticker_object.ticker}\n"
        message += f"<b>Company:</b> {ticker_object.company_name}\n"
        message += f"<b>Confidency:</b> {ticker_object.confidence}\n"

        await send_ws_message(
            {
                "name": "Citron Research",
                "type": "Buy",
                "ticker": ticker_object.ticker,
                "sender": "citron",
                "target": "CSS",
            },
            WS_SERVER_URL,
        )
        log_message(
            f"Report sent to Telegram and WebSocket for: {ticker_object.ticker} - {url}",
            "INFO",
        )
    else:
        log_message(f"Report sent to Telegram - {url}", "INFO")

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def run_scraper():
    processed_urls = load_processed_urls()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
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
                posts = await fetch_json(session)

                new_urls = [
                    post["source_url"]
                    for post in posts
                    if post.get("source_url")
                    and post["source_url"] not in processed_urls
                ]

                if new_urls:
                    log_message(f"Found {len(new_urls)} new posts to process.", "INFO")

                    for url in new_urls:
                        ticker_object = None
                        if url.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                            ticker_object = await analyze_image_for_ticker(url)
                        await send_to_telegram(url, ticker_object)
                        processed_urls.add(url)
                    save_processed_urls(processed_urls)
                else:
                    log_message("No new posts found.", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP, WS_SERVER_URL]):
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
