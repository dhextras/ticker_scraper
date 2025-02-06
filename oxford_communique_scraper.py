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
ARCHIVE_URL = "https://oxfordclub.com/publications/communique/?archive=issue"
LOGIN_URL = "https://oxfordclub.com/wp-login.php"
USERNAME = os.getenv("OXFORDCLUB_USERNAME")
PASSWORD = os.getenv("OXFORDCLUB_PASSWORD")
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/oxford_communique_processed_urls.json"
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
        json.dump(list(urls), f, indent=2)
    log_message("Processed URLs saved.", "INFO")


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


def clean_rec_text(text):
    # Clean the text by removing special characters except ':' and spaces
    pure_text = re.sub(r"[^a-zA-Z\s:]", " ", text)

    # Replace multiple spaces, new lines with a single space
    cleaned_text = re.sub(r"\s+", " ", pure_text.replace("\n", " "))

    return cleaned_text


def extract_tickers(rec_text):
    cleaned_text = clean_rec_text(rec_text)

    # Split into buy and sell sections
    sections = {}
    if "Buy" in cleaned_text:
        buy_text = re.split(
            r"\s+Sell\s+", cleaned_text[cleaned_text.index("Buy") :], maxsplit=1
        )
        sections["Buy"] = buy_text[0]
        if len(buy_text) > 1:
            sections["Sell"] = buy_text[1]
    elif "Sell" in cleaned_text:
        sections["Sell"] = cleaned_text[cleaned_text.index("Sell") :]

    results = {"Buy": [], "Sell": []}

    for action, section in sections.items():
        # Find all ticker matches
        matches = re.finditer(r"(NYSE|Nasdaq|CBOE|OTC):\s*([A-Z]+)", section)
        for match in matches:
            provider, ticker = match.groups()
            results[action].append((provider, ticker))

    return results


async def fetch_and_process_archive(session):
    cache_timestamp = int(time.time() * 10000)
    cache_uuid = uuid4()

    try:
        headers = {
            "Connection": "keep-alive",
            "cache-control": "no-cache, no-store, max-age=0, must-revalidate, private",
            "pragma": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36",
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
            "body > div.page-section.members-content > div > div > a.content-list-item-tall"
        )

        return [
            {
                "url": article["href"],
                "title": article.get_text().split("Recommendation")[0].strip(),
                "raw_recommendation": article.get_text()
                .split("Recommendation:")[-1]
                .strip(),
            }
            for article in articles
        ]

    except Exception as e:
        log_message(f"Error fetching archive: {e}", "ERROR")
        return []


async def send_to_telegram_and_ws(article_data, tickers_data):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )

    # Prepare ticker lists for proper message
    buy_tickers = [
        f"    - {provider}: {ticker}" for provider, ticker in tickers_data["Buy"]
    ]
    sell_tickers = [
        f"    - {provider}: {ticker}" for provider, ticker in tickers_data["Sell"]
    ]

    message = f"<b>New Communique Article Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Title:</b> {article_data['title']}\n"
    message += f"<b>URL:</b> {article_data['url']}\n"
    message += f"<b>Raw Recommendation:</b> {clean_rec_text(article_data['raw_recommendation'])}\n"
    if buy_tickers:
        message += f"Extracted Buy Tickers:\n{'\n'.join(buy_tickers)}\n"
    if sell_tickers:
        message += f"Extracted Sell Tickers:\n{'\n'.join(sell_tickers)}\n"

    for action in ["Buy", "Sell"]:
        for _, ticker in tickers_data[action]:
            await send_ws_message(
                {
                    "name": "OXFORD Communique",
                    "type": action,
                    "ticker": ticker,
                    "sender": "oxfordclub",
                    "target": "CSS",
                },
                WS_SERVER_URL,
            )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def run_scraper():
    processed_urls = load_processed_urls()

    session = requests.Session()
    if not login_sync(session):
        return

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new posts...", "DEBUG")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))

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
                    tickers_data = extract_tickers(article["raw_recommendation"])
                    await send_to_telegram_and_ws(article, tickers_data)
                    processed_urls.add(article["url"])

                save_processed_urls(processed_urls)
            else:
                log_message("No new articles found.", "INFO")

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
