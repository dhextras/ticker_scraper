import asyncio
import json
import os
import re
import sys
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
AUTHOR_URL = "https://app.stocks.news/author/stocks-news"
CHECK_INTERVAL = 1
SEARCH_WORD = "NASDAQ"
PROCESSED_JSON_FILE = "data/stocknews_html_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("STOCKNEWS_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("STOCKNEWS_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

os.makedirs("data", exist_ok=True)


async def fetch_blog_posts(session):
    try:
        async with session.get(AUTHOR_URL) as response:
            content = await response.text()
        soup = BeautifulSoup(content, "html.parser")

        blog_entries = []
        for blog_block in soup.find_all("div", class_="blog-list-block"):
            link = blog_block.find("a")
            title = (
                blog_block.find("h2").text.strip()
                if blog_block.find("h2")
                else "No title"
            )
            date = (
                blog_block.find("span", class_="date").text.strip()
                if blog_block.find("span", class_="date")
                else None
            )

            if link and link.get("href"):
                url = link["href"]
                blog_entries.append({"url": url, "title": title, "date": date})

        log_message(f"Fetched {len(blog_entries)} blog posts", "INFO")
        return blog_entries
    except Exception as e:
        log_message(f"Failed to fetch blog posts: {e}", "ERROR")
        return []


def load_processed_urls():
    try:
        with open(PROCESSED_JSON_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_processed_urls(urls):
    with open(PROCESSED_JSON_FILE, "w") as f:
        json.dump(urls, f)
        log_message("Processed URLs saved.", "INFO")


async def process_new_urls(session, new_entries):
    for entry in new_entries:
        await process_url(session, entry)


async def process_url(session, entry):
    url = entry["url"]
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
                post_title = entry["title"]
                post_date = entry["date"]
                log_message(f"Match found: {stock_symbol} in {url}", "INFO")
                await send_match_to_telegram(url, stock_symbol, post_title, post_date)
            else:
                log_message(f"No stock symbol match found in {url}", "WARNING")
        else:
            log_message(f"No match for search word in {url}", "WARNING")
    except Exception as e:
        log_message(f"Error processing URL {url}: {e}", "ERROR")


async def send_posts_to_telegram(entries, timestamp):
    urls = [entry["url"] for entry in entries]
    joined_urls = "\n  ".join(urls)

    message = f"<b>New Posts Found - HTML</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URLS:</b>\n  {joined_urls}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New Posts sent to Telegram: {urls}", "INFO")


async def send_match_to_telegram(url, stock_symbol, post_title, post_date):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S")
    message = f"<b>New Stock Match Found - HTML</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Stock Symbol:</b> {stock_symbol}\n"
    message += f"<b>Post Title:</b> {post_title}\n"
    message += f"<b>Post Date:</b> {post_date}\n"

    await send_ws_message(
        {
            "name": "Stock News H",
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
                current_entries = await fetch_blog_posts(session)

                # Check for new entries
                new_entries = []
                for entry in current_entries:
                    url = entry["url"]
                    date = entry["date"]
                    if url not in processed_urls or processed_urls[url] != date:
                        new_entries.append(entry)
                        processed_urls[url] = date

                if new_entries:
                    log_message(
                        f"Found {len(new_entries)} new blog posts. Processing...",
                        "INFO",
                    )
                    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    await send_posts_to_telegram(new_entries, timestamp)
                    await process_new_urls(session, new_entries)
                    save_processed_urls(processed_urls)
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
