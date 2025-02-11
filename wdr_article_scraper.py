import asyncio
import json
import os
import re
import sys
import time
from uuid import uuid4

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
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
ARCHIVE_URL = "https://whitediamondresearch.com/"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/white_diamond_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("WDR_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("WDR_TELEGRAM_GRP")
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


def extract_ticker(text):
    ticker_match = re.search(r"\(([A-Z]+)", text)
    return ticker_match.group(1) if ticker_match else None


async def fetch_and_process_archive(session):
    cache_timestamp = int(time.time() * 10000)
    cache_uuid = uuid4()

    try:
        headers = {
            "Connection": "keep-alive",
            "cache-control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "cache-timestamp": str(cache_timestamp),
            "cache-uuid": str(cache_uuid),
        }

        response = session.get(ARCHIVE_URL, headers=headers)
        if response.status_code != 200:
            log_message(
                f"Failed to fetch archive: HTTP {response.status_code}", "ERROR"
            )
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        articles = soup.select(
            "#primary > section > div.left-pside > div.researchs > article.posts"
        )

        processed_articles = []
        for article in articles:
            try:
                url = article.select_one("a")["href"]
                date = article.select_one(".date").text.strip()
                desc_text = article.select_one(".research-content").text.strip()
                title_text = article.select_one(".research-title a").text.strip()

                processed_articles.append(
                    {
                        "url": url,
                        "title": title_text,
                        "date": date,
                        "description": desc_text,
                        "ticker": extract_ticker(desc_text),
                    }
                )
            except:
                continue

        return processed_articles

    except Exception as e:
        log_message(f"Error fetching archive: {e}", "ERROR")
        return []


async def send_to_telegram_and_ws(article_data):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")

    message = f"<b>New White Diamond Article Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Article Date:</b> {article_data['date']}\n"
    message += f"<b>Title:</b> {article_data['title']}\n"
    message += f"<b>URL:</b> {article_data['url']}\n"
    message += f"<b>Description:</b> {article_data['description']}\n"

    if article_data["ticker"]:
        message += f"<b>Extracted Ticker:</b> {article_data['ticker']}\n"

    await send_ws_message(
        {
            "name": "White Diamond Article",
            "type": "Buy",
            "ticker": article_data["ticker"],
            "sender": "whitediamond",
            "target": "CSS",
        },
        WS_SERVER_URL,
    )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def run_scraper():
    processed_urls = load_processed_urls()
    session = requests.Session()

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

            log_message("Checking archive for new articles...")
            articles = await fetch_and_process_archive(session)

            new_articles = [
                article for article in articles if article["url"] not in processed_urls
            ]

            if new_articles:
                log_message(
                    f"Found {len(new_articles)} new articles to process.", "INFO"
                )

                for article in new_articles:
                    await send_to_telegram_and_ws(article)
                    processed_urls.add(article["url"])

                save_processed_urls(processed_urls)
            else:
                log_message("No new articles found.", "INFO")

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
