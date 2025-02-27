import asyncio
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime

import pytz
import requests
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
API_URL = "https://substack.com/api/v1/reader/posts"
CHECK_INTERVAL = 3  # seconds
PROCESSED_URLS_FILE = "data/substack_reader_processed_urls.json"
COOKIE_FILE = "cred/substack_cookies.json"
TELEGRAM_BOT_TOKEN = os.getenv("BEARCAVE_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BEARCAVE_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

os.makedirs("data", exist_ok=True)

# User agents list
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.59",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 OPR/78.0.4093.112",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/91.0.4472.80 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
]


def load_cookies():
    """Load substack.sid cookie from json file"""
    try:
        with open(COOKIE_FILE, "r") as f:
            data = json.load(f)
            return data.get("sid")
    except Exception as e:
        log_message(f"Error loading cookies: {e}", "ERROR")
        return None


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


def get_random_headers():
    """Generate random headers for requests"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": str(uuid.uuid4()),
        "X-Request-Time": str(int(time.time())),
    }


async def fetch_json(sid=None):
    """Fetch JSON data with custom headers"""
    if not sid:
        log_message("No Substack SID cookie available", "ERROR")
        return []

    headers = get_random_headers()
    cookies = {"substack.sid": sid}

    try:
        response = requests.get(f"{API_URL}?limit=10", headers=headers, cookies=cookies)
        if response.status_code == 200:
            data = response.json()

            log_message(f"Fetched posts from Reader API", "INFO")
            return data.get("posts", [])
        elif 500 <= response.status_code < 600:
            log_message(
                f"Server error {response.status_code}: Temporary issue, safe to ignore if infrequent."
                "WARNING",
            )
            return []

        else:
            log_message(f"Failed to fetch JSON: HTTP {response.status_code}", "ERROR")
            return []
    except Exception as e:
        log_message(f"Error fetching JSON: {e}", "ERROR")
        return []


def is_draft_post(url):
    """Check if the URL is a draft post"""
    return "/publish/post/" in url


def extract_ticker(title):
    if title is not None and title.find("Problems at") != -1:
        match = re.search(r"\((.*?)\)", title)
        if match:
            potential_ticker = match.group(1)
            if potential_ticker.isupper():
                return potential_ticker
    return None


def get_post_title(post):
    """Get the most appropriate title from the post data"""
    title = post.get("title", "")
    social_title = post.get("social_title", "")

    if not isinstance(title, str) or not title.strip():
        return (
            social_title
            if social_title
            else "No title found in either title/social_title"
        )
    return title


async def send_to_telegram(post_data, ticker=None):
    current_time = get_current_time()
    post_date = datetime.fromisoformat(post_data["post_date"].replace("Z", "+00:00"))
    post_date_est = post_date.astimezone(pytz.timezone("US/Eastern"))

    is_draft = is_draft_post(post_data.get("canonical_url", ""))
    title = post_data.get("title", "")
    social_title = post_data.get("social_title", "")

    message = f"<b>{'[DRAFT] ' if is_draft else ''}New Substack Reader bearcave Article!</b>\n\n"
    message += (
        f"<b>Published Date:</b> {post_date_est.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    )
    message += f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Social Title:</b> {social_title}\n"
    message += f"<b>URL:</b> {post_data['canonical_url']}\n"

    if ticker:
        message += f"<b>Ticker:</b> {ticker}\n"
        # TODO: Do it later after we verify its safe
        # await send_ws_message(
        #     {
        #         "name": "Bearcave - Reader",
        #         "type": "Sell",
        #         "ticker": ticker,
        #         "sender": "bearcave",
        #     },
        #     WS_SERVER_URL,
        # )
        # log_message(
        #     f"Ticker sent to WebSocket: {ticker} - {post_data['canonical_url']}", "INFO"
        # )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Article sent to Telegram: {post_data['canonical_url']}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()
    sid = load_cookies()

    if not sid:
        log_message("No Substack SID cookie available. Exiting.", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()
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
            posts = await fetch_json(sid)

            new_posts = [
                post
                for post in posts
                if post.get("canonical_url")
                and post["canonical_url"] not in processed_urls
            ]

            if new_posts:
                log_message(f"Found {len(new_posts)} new posts to process.", "INFO")

                for post in new_posts:
                    if str(post.get("", "")) != "26828":
                        continue

                    title = get_post_title(post)
                    ticker = extract_ticker(title)
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
