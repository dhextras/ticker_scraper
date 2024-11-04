import asyncio
import json
import os
import re
import sys
from datetime import datetime

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
JSON_URL = "https://oxfordclub.com/wp-json/wp/v2/posts"
LOGIN_URL = "https://oxfordclub.com/wp-login.php"
USERNAME = os.getenv("OXFORDCLUB_USERNAME")
PASSWORD = os.getenv("OXFORDCLUB_PASSWORD")
CHECK_INTERVAL = 0.2  # seconds
PROCESSED_URLS_FILE = "data/oxfordclub_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("OXFORDCLUB_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("OXFORDCLUB_TELEGRAM_GRP")
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
        json.dump(list(urls), f)
    log_message("Processed URLs saved.", "INFO")


def fetch_json(session):
    try:
        response = session.get(JSON_URL)
        if response.status_code == 200:
            data = response.json()
            log_message(f"Fetched {len(data)} posts from JSON", "INFO")
            return data
        else:
            log_message(f"Failed to fetch JSON: HTTP {response.status}", "ERROR")
            return []
    except Exception as e:
        log_message(f"Error fetching JSON: {e}", "ERROR")
        return []


def login_sync(session):
    try:
        payload = {"log": USERNAME, "pwd": PASSWORD}
        response = session.post(LOGIN_URL, data=payload)
        if response.status_code == 200:
            log_message("Login successful", "INFO")
            return True
        else:
            log_message(f"Login failed: HTTP {response.status_code}", "ERROR")
            return False
    except Exception as e:
        log_message(f"Error during login: {e}", "ERROR")
        return False


async def process_page(session, url):
    try:
        headers = {
            "Connection": "keep-alive",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36",
        }
        response = session.get(url, headers=headers)
        if response.status_code == 200:
            content = response.text
            soup = BeautifulSoup(content, "html.parser")
            all_text = soup.get_text(separator=" ", strip=True)

            action_sections = re.split(r"Action to Take", all_text, flags=re.IGNORECASE)

            if len(action_sections) < 2:
                log_message(f"'Action to Take' not found: {url}", "WARNING")

            for section in action_sections[1:]:
                buy_match = re.search(r"Buy", section, re.IGNORECASE)
                ticker_match = re.search(
                    r"(NYSE|NASDAQ):\s*(\w+)", section, re.IGNORECASE
                )

                if (
                    buy_match
                    and ticker_match
                    and buy_match.start() < ticker_match.start()
                ):
                    exchange, ticker = ticker_match.groups()
                    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    await send_match_to_telegram(url, ticker, exchange, timestamp)
                    break
                elif not ticker_match:
                    log_message(f"No ticker found in section: {url}", "WARNING")
                elif not buy_match or (
                    buy_match
                    and ticker_match
                    and buy_match.start() > ticker_match.start()
                ):
                    log_message(
                        f"'Buy' not found before ticker in section: {url}", "WARNING"
                    )
        else:
            log_message(f"Failed to fetch page: HTTP {response.status_code}", "ERROR")
    except Exception as e:
        log_message(f"Error processing page {url}: {e}", "ERROR")


async def send_posts_to_telegram(urls, timestamp):
    joined_urls = "\n  ".join(urls)

    message = f"<b>New Posts Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URLS:</b>\n  {joined_urls}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New Posts sent to Telegram: {urls}", "INFO")


async def send_match_to_telegram(url, ticker, exchange, timestamp):
    message = f"<b>New Stock Match Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Stock Symbol:</b> {exchange}:{ticker}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    await send_ws_message(
        {
            "name": "Oxford Club",
            "type": "Buy",
            "ticker": ticker,
            "sender": "oxfordclub",
        },
        WS_SERVER_URL,
    )
    log_message(
        f"Match sent to Telegram and WebSocket: {exchange}:{ticker} - {url}", "INFO"
    )


async def run_scraper():
    processed_urls = load_processed_urls()

    session = requests.Session()
    if not login_sync(session):
        return

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new posts...")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))

            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...")
                break

            log_message("Checking for new posts...")
            posts = fetch_json(session)

            new_urls = [
                post["link"]
                for post in posts
                if post.get("link") and post["link"] not in processed_urls
            ]

            if new_urls:
                log_message(f"Found {len(new_urls)} new posts to process.", "INFO")
                timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                for url in new_urls:
                    await process_page(session, url)
                    processed_urls.add(url)

                await send_posts_to_telegram(new_urls, timestamp)
                save_processed_urls(processed_urls)
            else:
                log_message("No new posts found.", "INFO")

            await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([USERNAME, PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP, WS_SERVER_URL]):
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
