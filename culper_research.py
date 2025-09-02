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
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

CULPER_URL = "https://culperresearch.com/latest-research"
CHECK_INTERVAL = 1
PROCESSED_URLS_FILE = "data/culper_research_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("CULPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("CULPER_TELEGRAM_GRP")

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


def extract_ticker(title_text):
    title_text = re.sub(r"\s*\(pdf\)\s*$", "", title_text, flags=re.IGNORECASE)

    nasdaq_match = re.search(r"\(NASDAQ:([A-Z]+)\)", title_text)
    if nasdaq_match:
        return nasdaq_match.group(1)

    nyse_match = re.search(r"\(NYSE:([A-Z]+)\)", title_text)
    if nyse_match:
        return nyse_match.group(1)

    bracket_match = re.search(r"\(([A-Z]+)\)", title_text)
    if bracket_match:
        return bracket_match.group(1)

    return None


async def fetch_and_process_culper(session):
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

        response = session.get(CULPER_URL, headers=headers)
        if 500 <= response.status_code < 600:
            log_message(
                f"Server error {response.status_code}: Temporary issue, safe to ignore if infrequent.",
                "WARNING",
            )
            return []
        elif response.status_code != 200:
            log_message(
                f"Failed to fetch Culper Research: HTTP {response.status_code}", "ERROR"
            )
            return []

        soup = BeautifulSoup(response.text, "html.parser")

        download_links = soup.select(
            'a[data-aid="DOWNLOAD_DOCUMENT_LINK_WRAPPER_RENDERED"]'
        )

        processed_articles = []

        for link in download_links:
            try:
                span = link.select_one("span:first-child")
                if not span:
                    continue

                title_text = span.text.strip()
                ticker = extract_ticker(title_text)

                if not ticker:
                    continue

                pdf_url = link.get("href", "")
                if pdf_url.startswith("//"):
                    pdf_url = "https:" + pdf_url

                processed_articles.append(
                    {
                        "url": pdf_url,
                        "title": title_text,
                        "ticker": ticker,
                    }
                )

            except Exception as e:
                log_message(f"Error processing download link: {e}", "WARNING")
                continue

        return processed_articles

    except Exception as e:
        log_message(f"Error fetching Culper Research: {e}", "ERROR")
        return []


async def send_to_telegram_and_ws(article_data):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")

    message = f"<b>New Culper Research Article Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Title:</b> {article_data['title']}\n"
    message += f"<b>PDF URL:</b> {article_data['url']}\n"
    message += f"<b>Extracted Ticker:</b> {article_data['ticker']}\n"

    await send_ws_message(
        {
            "name": "Culper Research Article",
            "type": "Sell",
            "ticker": article_data["ticker"],
            "sender": "culperresearch",
        },
    )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)

    log_message(
        f"Found a new pdf: {article_data['url']}, found ticker: {article_data['ticker']}"
    )


async def run_scraper():
    processed_urls = load_processed_urls()
    session = requests.Session()

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

            log_message("Checking Culper Research for new articles...")
            articles = await fetch_and_process_culper(session)

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
