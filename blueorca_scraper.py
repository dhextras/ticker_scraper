import asyncio
import io
import json
import os
import re
import sys

import aiohttp
from dotenv import load_dotenv
from pdfminer.high_level import extract_text

from utils.gpt_ticker_extractor import TickerAnalysis, analyze_image_for_ticker
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
JSON_URL = "https://www.blueorcacapital.com/wp-json/wp/v2/media"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/blueorca_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("BLUEORCA_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BLUEORCA_TELEGRAM_GRP")

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
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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
            else:
                log_message(f"Failed to fetch JSON: HTTP {response.status}", "ERROR")
                return []
    except Exception as e:
        log_message(f"Error fetching JSON: {e}", "ERROR")
        return []


async def extract_ticker_from_pdf(session, url):
    try:
        async with session.get(url) as response:
            if response.status == 200:
                pdf_content = await response.read()
                pdf_file = io.BytesIO(pdf_content)

                first_page = extract_text(pdf_file, page_numbers=[0])

                # Check for format like "NYSE: TDOC" or "NASDAQ: GDS"
                ticker_pattern = r"(?:NYSE|NASDAQ|AMEX|ASX|KOSDAQ|HK):\s*([A-Z0-9]+)"
                match = re.search(ticker_pattern, first_page)
                if match:
                    return match.group(1)

                # Look for capitalized words after brackets
                bracket_pattern = r"\((.*?)\)"
                matches = re.findall(bracket_pattern, first_page)

                if matches:
                    # Take the first all-caps word after a bracket
                    for match in matches:
                        if match.isupper():
                            return match

                log_message(f"No ticker found in PDF: {url}", "WARNING")
                return None
            else:
                log_message(f"Failed to fetch PDF: HTTP {response.status}", "ERROR")
                return None
    except Exception as e:
        log_message(f"Error processing PDF {url}: {e}", "ERROR")
        return None


async def send_posts_to_telegram(urls):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
    joined_urls = "\n  ".join(urls)

    message = f"<b>New Blue Orca medias found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Media URLS:</b>\n  {joined_urls}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New Posts sent to Telegram: {urls}", "INFO")


async def send_to_telegram(url, ticker_obj: TickerAnalysis | str):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Blue Orca Ticker found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"

    if isinstance(ticker_obj, str):
        ticker = ticker_obj
        message += f"<b>Ticker:</b> {ticker_obj}\n"
    else:
        ticker = ticker_obj.ticker
        message += f"\n<b>Ticker:</b> {ticker_obj.ticker}\n"
        message += f"<b>Company:</b> {ticker_obj.company_name}\n"
        message += f"<b>Confidency:</b> {ticker_obj.confidence}\n"

    await send_ws_message(
        {
            "name": "Blue Orca Capital",
            "type": "Sell",
            "ticker": ticker,
            "sender": "blueorca",
        },
    )
    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Report sent to Telegram and WebSocket: {ticker} - {url}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()

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
                        if url.lower().endswith(".pdf"):
                            ticker = await extract_ticker_from_pdf(session, url)
                            if ticker:
                                await send_to_telegram(url, ticker_obj=ticker)
                        elif url.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                            ticker_object = await analyze_image_for_ticker(url)
                            if ticker_object and ticker_object.found:
                                await send_to_telegram(url, ticker_obj=ticker_object)

                        processed_urls.add(url)

                    await send_posts_to_telegram(new_urls)
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
