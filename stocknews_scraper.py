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
SITEMAP_URL = "https://app.stocks.news/blog-sitemap.xml"
CHECK_INTERVAL = 1
SEARCH_WORD = "NASDAQ"
PROCESSED_JSON_FILE = "data/stocknews_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("STOCKNEWS_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("STOCKNEWS_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
CONTENT_SNIPPET_LENGTH = 3000  # Number of characters to save for content comparison

os.makedirs("data", exist_ok=True)


async def fetch_sitemap(session):
    """
    Fetch URLs from the sitemap.

    Args:
        session (aiohttp.ClientSession): The aiohttp client session

    Returns:
        list: List of URLs from the sitemap
    """
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
    """
    Load previously processed URLs from JSON file.

    Returns:
        dict: Dictionary of processed URLs
    """
    try:
        with open(PROCESSED_JSON_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_processed_urls(urls):
    """
    Save processed URLs to JSON file.

    Args:
        urls (dict): Dictionary of processed URLs
    """
    with open(PROCESSED_JSON_FILE, "w") as f:
        json.dump(urls, f, indent=2)
        log_message("Processed URLs saved.", "INFO")


async def fetch_blog_content(session, url):
    """
    Fetch the first 1000 characters of a blog post asynchronously.

    Args:
        session (aiohttp.ClientSession): The aiohttp client session
        url (str): URL of the blog post

    Returns:
        dict: A dictionary containing url, content snippet, and title
    """
    try:
        async with session.get(url) as response:
            content = await response.text()

        soup = BeautifulSoup(content, "html.parser")
        text_content = soup.get_text(strip=True)
        post_title = soup.title.string if soup.title else "No title found"

        # Remove the realated blog post to avoid duplicate post sending
        if "Related Blogs" in text_content:
            text_content = text_content.split("Related Blogs")[0]

        # Truncate content to specified length
        content_snippet = text_content[:CONTENT_SNIPPET_LENGTH]

        return {"url": url, "content_snippet": content_snippet, "title": post_title}
    except Exception as e:
        log_message(f"Error fetching content for {url}: {e}", "ERROR")
        return {
            "url": url,
            "content_snippet": None,
            "title": "No title found",
            "error": str(e),
        }


async def process_new_entries(session, new_urls, processed_urls):
    """
    Asynchronously process new blog entries, checking content changes and potential stock matches.

    Args:
        session (aiohttp.ClientSession): The aiohttp client session
        new_urls (list): List of new blog URLs
        processed_urls (dict): Dictionary of previously processed URLs

    Returns:
        list: List of entries that have changed content but doesn't have a ticker in it
    """
    # Create tasks for fetching content of all new URLs
    content_tasks = [fetch_blog_content(session, url) for url in new_urls]
    contents = await asyncio.gather(*content_tasks)

    changed_entries = []
    for content_info in contents:
        url = content_info["url"]

        if not content_info.get("content_snippet"):
            continue

        current_snippet = content_info["content_snippet"]
        if (
            url not in processed_urls
            or processed_urls[url].get("content_snippet") != current_snippet
        ):
            processed_urls[url] = {"content_snippet": current_snippet}

            # Check for NASDAQ match
            if SEARCH_WORD in current_snippet:
                match = re.search(r"NASDAQ:\s+([A-Z]+)", current_snippet, re.IGNORECASE)
                if match:
                    stock_symbol = match.group(1)
                    log_message(f"Match found: {stock_symbol} in {url}", "INFO")

                    await send_match_to_telegram(
                        url, stock_symbol, content_info["title"]
                    )
                    continue

            changed_entries.append({"url": url, "title": content_info["title"]})

    return changed_entries


async def send_match_to_telegram(url, stock_symbol, post_title):
    """
    Send a match notification to Telegram and WebSocket.

    Args:
        url (str): URL of the blog post
        stock_symbol (str): Matched stock symbol
        post_title (str): Title of the blog post
    """
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S")
    message = f"<b>New Stock Match Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Stock Symbol:</b> {stock_symbol}\n"
    message += f"<b>Post Title:</b> {post_title}\n"

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
    """
    Main scraper run loop that checks for new blog posts during market hours.
    """
    processed_urls = load_processed_urls()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            log_message(
                "Market is open. Starting to check for new blog posts...", "DEBUG"
            )
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = datetime.now(pytz.timezone("America/New_York"))

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking for new blog posts...")
                current_urls = await fetch_sitemap(session)

                if current_urls:
                    log_message(
                        f"Found {len(current_urls)} new blog posts. Processing...",
                        "INFO",
                    )

                    changed_entries = await process_new_entries(
                        session, current_urls, processed_urls
                    )

                    if changed_entries:
                        timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        await send_posts_to_telegram(changed_entries, timestamp)

                    processed_urls = {
                        url: processed_urls.get(url, {}) for url in current_urls
                    }
                    save_processed_urls(processed_urls)

                await asyncio.sleep(CHECK_INTERVAL)


async def send_posts_to_telegram(entries, timestamp):
    """
    Send a list of new blog post URLs to Telegram.

    Args:
        entries (list): List of blog post entries
        timestamp (str): Timestamp of the check
    """
    urls = [entry["url"] for entry in entries]
    joined_urls = "\n  ".join(urls)

    message = f"<b>New Posts Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URLS:</b>\n  {joined_urls}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New Posts sent to Telegram: {urls}", "INFO")


def main():
    """
    Main function to run the scraper with error handling.
    """
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
