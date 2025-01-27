import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from uuid import uuid4

import pytz
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
ARCHIVE_URL = "https://whitediamondresearch.com/"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/white_diamond_table_processed_urls.json"
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


def extract_ticker_and_sentiment(text):
    # Extract ticker and sentiment from text like "TELO/Bearish" or "AUID/Bearish (3rd call)"
    match = re.match(r"([A-Z]+)/(\w+)", text)
    if match:
        return match.group(1), match.group(2).lower()
    return None, None


async def fetch_and_process_table(session):
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
        rows = soup.select("#primary > section > div.right-pside > table > tbody > tr")

        entries = []
        for row in rows:
            link_elem = row.select_one("td:first-child a")
            if not link_elem:
                continue

            ticker, sentiment = extract_ticker_and_sentiment(link_elem.text)
            if not ticker:
                continue

            date_elem = row.select_one("td:nth-child(2)")
            if not date_elem:
                continue

            date = date_elem.text.strip()
            url = link_elem["href"]

            entries.append(
                {"url": url, "ticker": ticker, "sentiment": sentiment, "date": date}
            )

        return entries

    except Exception as e:
        log_message(f"Error fetching table: {e}", "ERROR")
        return []


async def send_to_telegram_and_ws(entry_data):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )
    ws_type = "Sell" if entry_data["sentiment"] == "bearish" else "Buy"

    message = f"<b>New White Diamond Research Table Entry Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Entry Date:</b> {entry_data['date']}\n"
    message += f"<b>Ticker:</b> {entry_data['ticker']}\n"
    message += f"<b>Sentiment:</b> {entry_data['sentiment']}\n"
    message += f"<b>URL:</b> {entry_data['url']}\n"

    # TODO: Implement websocket sending
    # await send_ws_message(
    #     {
    #         "name": "White Diamond Table",
    #         "type": ws_type,
    #         "ticker": entry_data['ticker'],
    #         "sender": "whitediamond",
    #     },
    #     WS_SERVER_URL,
    # )

    print(message)
    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def run_scraper():
    processed_urls = load_processed_urls()
    session = requests.Session()

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new entries...")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))

            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...")
                break

            log_message("Checking table for new entries...")
            entries = await fetch_and_process_table(session)

            new_entries = [
                entry for entry in entries if entry["url"] not in processed_urls
            ]

            if new_entries:
                log_message(f"Found {len(new_entries)} new entries to process.", "INFO")

                for entry in new_entries:
                    await send_to_telegram_and_ws(entry)
                    processed_urls.add(entry["url"])

                save_processed_urls(processed_urls)
            else:
                log_message("No new entries found.", "INFO")

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
