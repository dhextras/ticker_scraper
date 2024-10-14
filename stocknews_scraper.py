import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import pytz
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from telegram_bot import send_to_group

load_dotenv()
# Constants
SITEMAP_URL = "https://app.stocks.news/blog-sitemap.xml?page=1"
CHECK_INTERVAL = 5
SEARCH_WORD = "NASDAQ"
PROCESSED_JSON_FILE = "data/stocknews_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.environ.get("STOCKNEWS_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.environ.get("STOCKNEWS_TELEGRAM_GRP")

print(TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
# Color codes
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def log_message(message, level="INFO"):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S")
    color = {"ERROR": RED, "SUCCESS": GREEN, "WARNING": YELLOW}.get(level, "")

    print(
        f"{color}[{timestamp}] {level}: {message}{RESET}"
        if color
        else f"\n[{timestamp}] {message}"
    )


def fetch_sitemap():
    try:
        response = requests.get(SITEMAP_URL)
        root = ET.fromstring(response.content)
        urls = [
            url.text
            for url in root.findall(
                ".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
            )
        ]
        log_message(f"Fetched {len(urls)} URLs from sitemap", "SUCCESS")
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
        log_message("Processed URLs saved.", "SUCCESS")


async def process_new_urls(new_urls):
    for url in new_urls:
        await process_url(url)


async def process_url(url):
    log_message(f"Processing URL: {url}")
    try:
        response = requests.get(url)
        soup = BeautifulSoup(response.text, "html.parser")
        content = soup.get_text()

        if SEARCH_WORD in content:
            match = re.search(r"NASDAQ:\s+([A-Z]+)", content, re.IGNORECASE)
            if match:
                stock_symbol = match.group(1)
                post_title = soup.title.string if soup.title else "No title found"
                post_date = "not fetching at the moment"
                log_message(f"Match found: {stock_symbol} in {url}", "SUCCESS")
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

    await send_to_group(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New Posts sent to Telegram: {urls}", "SUCCESS")


async def send_match_to_telegram(url, stock_symbol, post_title, post_date):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S")
    message = f"<b>New Stock Match Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Stock Symbol:</b> {stock_symbol}\n"
    message += f"<b>Post Title:</b> {post_title}\n"
    message += f"<b>Post Date:</b> {post_date}\n"

    await send_to_group(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Match sent to Telegram: {stock_symbol} - {url}", "SUCCESS")


async def main():
    processed_urls = load_processed_urls()

    while True:
        log_message("Checking for new blog posts...")
        current_urls = fetch_sitemap()
        new_urls = [url for url in current_urls if url not in processed_urls]

        if new_urls:
            log_message(
                f"Found {len(new_urls)} new blog posts. Processing...", "SUCCESS"
            )
            timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            await send_posts_to_telegram(new_urls, timestamp)

            await process_new_urls(new_urls)
            processed_urls.extend(new_urls)
            save_processed_urls(processed_urls)
        else:
            log_message("No new blog posts found.", "WARNING")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
