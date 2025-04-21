import asyncio
import json
import os
import re
import sys

import aiohttp
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

# Constants
AUTHOR_URL = "https://app.stocks.news/blogs"
CHECK_INTERVAL = 0.1
PROCESSED_JSON_FILE = "data/stocknews_html_processed_urls.json"
TICKER_LIST_FILE = "data/stocksnews_processed_tickers.json"
TELEGRAM_BOT_TOKEN = os.getenv("STOCKNEWS_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("STOCKNEWS_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


async def fetch_blog_posts(session):
    try:
        async with session.get(AUTHOR_URL) as response:
            content = await response.text()
        soup = BeautifulSoup(content, "html.parser")

        blog_entries = []
        for blog_block in soup.find_all("div", class_="element-1"):
            link = blog_block.find("a")
            title = (
                blog_block.find("h4").text.strip()
                if blog_block.find("h4")
                else "No title"
            )

            if link and link.get("href"):
                url = link["href"]
                blog_entries.append({"url": url, "title": title})

        log_message(f"Fetched {len(blog_entries)} blog posts from html", "INFO")
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
        json.dump(urls, f, indent=2)


def load_processed_tickers():
    try:
        with open(TICKER_LIST_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_processed_tickers(tickers):
    with open(TICKER_LIST_FILE, "w") as f:
        json.dump(tickers, f, indent=2)


async def fetch_blog_content(session, url):
    """
    Fetch the content of a blog post asynchronously to check for stock symbols.

    Args:
        session (aiohttp.ClientSession): The aiohttp client session
        url (str): URL of the blog post

    Returns:
        dict: A dictionary containing url and text content
    """
    try:
        async with session.get(url) as response:
            content = await response.text()

        soup = BeautifulSoup(content, "html.parser")
        text_content = soup.get_text(strip=True)

        # Remove the related blog posts to avoid duplicate post sending
        if "Related Blogs" in text_content:
            text_content = text_content.split("Related Blogs")[0]

        return {"url": url, "text_content": text_content}
    except Exception as e:
        log_message(f"Error fetching content for {url}: {e}", "ERROR")
        return {"url": url, "text_content": None, "error": str(e)}


async def process_new_entries(session, current_entries, processed_urls):
    """
    Asynchronously process blog entries, checking for new or changed titles.

    Args:
        session (aiohttp.ClientSession): The aiohttp client session
        current_entries (list): List of current blog entries
        processed_urls (dict): Dictionary of previously processed URLs

    Returns:
        list: List of entries that are new or have changed titles
    """
    processed_tickers = load_processed_tickers()
    new_or_changed_entries = []
    content_fetch_entries = []

    # First pass: identify new or changed titles
    for entry in current_entries:
        url = entry["url"]
        title = entry["title"]

        # Skip posts with "update" or "sponsored" in the title
        if "update" in title.lower() or "sponsored" in title.lower():
            log_message(f"Skipping update/sponsored post: {title}", "INFO")
            continue

        if url not in processed_urls or processed_urls[url].get("title") != title:
            new_or_changed_entries.append(entry)
            content_fetch_entries.append(entry)

            if url not in processed_urls:
                processed_urls[url] = {}
            processed_urls[url]["title"] = title

    if not content_fetch_entries:
        return []

    # Only fetch content for entries that are new or have changed titles
    content_tasks = [
        fetch_blog_content(session, entry["url"]) for entry in content_fetch_entries
    ]
    contents = await asyncio.gather(*content_tasks)

    changed_entries = []
    for entry, content_info in zip(content_fetch_entries, contents):
        url = entry["url"]
        title = entry["title"]

        if not content_info.get("text_content"):
            continue

        text_content = content_info["text_content"]
        stock_symbol = None

        # Check for stock symbols in title
        nasdaq_ticker_match = re.search(
            r"(?:NASDAQ|NYSE)[:;]\s*([A-Z]+)", title, re.IGNORECASE
        )
        if nasdaq_ticker_match:
            stock_symbol = nasdaq_ticker_match.group(1)
            log_message(
                f"Match found in title with NASDAQ/NYSE format: {stock_symbol} in {url}",
                "INFO",
            )

        # Check for stock symbols in content if none found in title
        if not stock_symbol and (
            "nasdaq" in text_content.lower() or "nyse" in text_content.lower()
        ):
            match = re.search(
                r"(?:NASDAQ|NYSE)[:;]\s+([A-Z]+)",
                text_content,
                re.IGNORECASE,
            )
            if match:
                stock_symbol = match.group(1)
                log_message(f"Match found in content: {stock_symbol} in {url}", "INFO")

        if stock_symbol:
            if stock_symbol in processed_tickers:
                log_message(
                    f"Ticker {stock_symbol} already processed, skipping", "INFO"
                )
                continue

            processed_tickers.append(stock_symbol)
            save_processed_tickers(processed_tickers)

            await send_match_to_telegram(url, stock_symbol, title)
            continue

        changed_entries.append({"url": url, "title": title})

    return changed_entries


async def send_match_to_telegram(url, stock_symbol, post_title):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
    message = f"<b>New Stock Match Found - HTML</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Stock Symbol:</b> {stock_symbol}\n"
    message += f"<b>Post Title:</b> {post_title}\n"

    await send_ws_message(
        {
            "name": "Stock News H",
            "type": "Buy",
            "ticker": stock_symbol,
            "sender": "stocknews",
        },
    )
    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Match sent to Telegram and WebSocket: {stock_symbol} - {url}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message(
                "Market is open. Starting to check for new blog posts...", "DEBUG"
            )
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking for new blog posts...")
                current_entries = await fetch_blog_posts(session)

                if current_entries:
                    log_message(
                        f"Found {len(current_entries)} blog posts. Processing...",
                        "INFO",
                    )

                    changed_entries = await process_new_entries(
                        session, current_entries, processed_urls
                    )

                    if changed_entries:
                        timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
                        await send_posts_to_telegram(changed_entries, timestamp)

                    save_processed_urls(processed_urls)
                else:
                    log_message("No new blog posts found.", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


async def send_posts_to_telegram(entries, timestamp):
    url_titles = [f"{entry['url']} - {entry['title']}" for entry in entries]
    joined_url_titles = "\n  ".join(url_titles)
    message = f"<b>New Posts Found - HTML</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URLS:</b>\n  {joined_url_titles}"
    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)

    for entry in entries:
        log_message(f"New Post: {entry['url']} - {entry['title']}", "INFO")

    log_message(f"New Posts sent to Telegram: {len(entries)}", "INFO")


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
