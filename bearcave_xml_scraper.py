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
XML_FEED_URL = "https://thebearcave.substack.com/feed"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/bearcave_xml_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("BEARCAVE_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BEARCAVE_TELEGRAM_GRP")
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


def fetch_xml_feed():
    try:
        response = requests.get(XML_FEED_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "xml")

        posts = []
        for item in soup.find_all("item"):
            # Clean CDATA title
            title = item.find("title").text.strip()
            title = re.sub(
                r"^\s*\[\[CDATA\[(.*?)\]\]\s*$", r"\1", title, flags=re.DOTALL
            )

            # Parse date
            pub_date_str = item.find("pubDate").text.strip()
            pub_date = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %Z")
            pub_date_iso = pub_date.isoformat() + "Z"

            posts.append(
                {
                    "title": title,
                    "canonical_url": item.find("link").text.strip(),
                    "post_date": pub_date_iso,
                }
            )

        log_message(f"Fetched {len(posts)} posts from XML feed", "INFO")
        return posts
    except Exception as e:
        log_message(f"Error fetching XML feed: {e}", "ERROR")
        return []


def extract_ticker(title):
    if title is not None and title.startswith("Problems at"):
        # Look for text within parentheses
        match = re.search(r"\((.*?)\)", title)
        if match:
            potential_ticker = match.group(1)
            # Verify it's all uppercase
            if potential_ticker.isupper():
                return potential_ticker
    return None


async def send_to_telegram(post_data, ticker=None):
    current_time = datetime.now(pytz.timezone("US/Eastern"))
    post_date = datetime.fromisoformat(post_data["post_date"].replace("Z", "+00:00"))
    post_date_est = post_date.astimezone(pytz.timezone("US/Eastern"))

    message = f"<b>New Bear Cave Article - XML!</b>\n\n"
    message += (
        f"<b>Published Date:</b> {post_date_est.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    )
    message += f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Title:</b> {post_data['title']}\n"
    message += f"<b>URL:</b> {post_data['canonical_url']}\n"

    if ticker:
        message += f"<b>Ticker:</b> {ticker}\n"
        await send_ws_message(
            {
                "name": "The Bear Cave - X",
                "type": "Sell",
                "ticker": ticker,
                "sender": "bearcave",
            },
            WS_SERVER_URL,
        )
        log_message(
            f"Ticker sent to WebSocket: {ticker} - {post_data['canonical_url']}", "INFO"
        )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Article sent to Telegram: {post_data['canonical_url']}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()

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
            posts = fetch_xml_feed()

            new_posts = [
                post
                for post in posts
                if post.get("canonical_url")
                and post["canonical_url"] not in processed_urls
            ]

            if new_posts:
                log_message(f"Found {len(new_posts)} new posts to process.", "INFO")

                for post in new_posts:
                    if not "title" in post:
                        continue

                    ticker = extract_ticker(post["title"])
                    await send_to_telegram(post, ticker)
                    processed_urls.add(post["canonical_url"])

                save_processed_urls(processed_urls)
            else:
                log_message("No new posts found.", "INFO")

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
