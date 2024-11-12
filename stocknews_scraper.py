import asyncio
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
SITEMAP_URL = "https://app.stocks.news/blog-sitemap.xml?page=1"
CHECK_INTERVAL = 1
SEARCH_WORD = "NASDAQ"
PROCESSED_JSON_FILE = "data/stocknews_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("STOCKNEWS_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("STOCKNEWS_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

os.makedirs("data", exist_ok=True)


async def fetch_sitemap(session):
    try:
        async with session.get(SITEMAP_URL) as response:
            content = await response.text()
        root = ET.fromstring(content)
        urls = [
            url.text
            for url in root.findall(
                ".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
            )
        ]
        log_message(f"Fetched {len(urls)} URLs from sitemap", "INFO")
        return urls
    except Exception as e:
        log_message(f"Failed to fetch sitemap: {e}", "ERROR")
        return []


def load_processed_urls():
    try:
        with open(PROCESSED_JSON_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_processed_urls(urls):
    with open(PROCESSED_JSON_FILE, "w") as f:
        json.dump(urls, f)
        log_message("Processed URLs saved.", "INFO")


async def process_new_urls(session, new_urls):
    for url in new_urls:
        await process_url(session, url)


async def process_url(session, url):
    log_message(f"Processing URL: {url}")
    try:
        async with session.get(url) as response:
            content = await response.text()
        soup = BeautifulSoup(content, "html.parser")
        content = soup.get_text()

        if SEARCH_WORD in content:
            match = re.search(r"NASDAQ:\s+([A-Z]+)", content, re.IGNORECASE)
            if match:
                stock_symbol = match.group(1)
                post_title = soup.title.string if soup.title else "No title found"
                post_date = "not fetching at the moment"
                log_message(f"Match found: {stock_symbol} in {url}", "INFO")
                await send_match_to_telegram(url, stock_symbol, post_title, post_date)
            else:
                log_message(f"No stock symbol match found in {url}", "WARNING")
        else:
            log_message(f"No match for search word in {url}", "WARNING")
    except Exception as e:
        log_message(f"Error processing URL {url}: {e}", "ERROR")


async def send_posts_to_telegram(urls, timestamp):
    joined_urls = "\n  ".join(urls)

    message = f"<b>New Posts Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URLS:</b>\n  {joined_urls}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New Posts sent to Telegram: {urls}", "INFO")


async def send_match_to_telegram(url, stock_symbol, post_title, post_date):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S")
    message = f"<b>New Stock Match Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Stock Symbol:</b> {stock_symbol}\n"
    message += f"<b>Post Title:</b> {post_title}\n"
    message += f"<b>Post Date:</b> {post_date}\n"

    await send_ws_message(
        {
            "name": "Stock News",
            "type": "Buy",
            "ticker": stock_symbol,
            "sender": "stocknews",
        },
        WS_SERVER_URL,
    )
    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Match sent to Telegram and WebSocket: {stock_symbol} - {url}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            log_message("Market is open. Starting to check for new blog posts...")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = datetime.now(pytz.timezone("America/New_York"))

                if current_time > market_close_time:
                    log_message("Market is closed. Waiting for next market open...")
                    break

                log_message("Checking for new blog posts...")
                current_urls = await fetch_sitemap(session)
                new_urls = [url for url in current_urls if url not in processed_urls]

                if new_urls:
                    log_message(
                        f"Found {len(new_urls)} new blog posts. Processing...", "INFO"
                    )
                    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    await send_posts_to_telegram(new_urls, timestamp)

                    await process_new_urls(session, new_urls)

                    # Only keep the list of current urls avaible in the sitemap
                    save_processed_urls(current_urls)
                else:
                    log_message("No new blog posts found.", "INFO")

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
