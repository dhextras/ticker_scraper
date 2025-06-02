import asyncio
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

import aiohttp
import pytz
from dotenv import load_dotenv

from utils.bypass_cloudflare import bypasser
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()

# Constants
JSON_URL = "https://jcapitalresearch.substack.com/api/v1/posts"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/jcapital_processed_urls.json"
SESSION_FILE = "data/jcapital_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("JCAPITAL_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("JCAPITAL_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_cookies(fresh=False) -> Optional[Dict[str, Any]]:
    try:
        cookies = None
        if fresh == False:
            if not os.path.exists(SESSION_FILE):
                log_message(f"Session file not found: {SESSION_FILE}", "WARNING")
            else:
                with open(SESSION_FILE, "r") as f:
                    cookies = json.load(f)

        if not cookies or cookies.get("cf_clearance", "") == "":
            log_message(
                "Invalid or missing 'cf_clearance' in cookies. Attempting to regenerate.",
                "WARNING",
            )
            bypass = bypasser(JSON_URL, SESSION_FILE)

            if not bypass or bypass == False:
                return None

            with open(SESSION_FILE, "r") as f:
                cookies = json.load(f)

            if not cookies or cookies.get("cf_clearance", "") == "":
                return None

        return cookies

    except json.JSONDecodeError:
        log_message("Failed to decode JSON from session file.", "ERROR")
    except Exception as e:
        log_message(f"Error loading session: {e}", "ERROR")

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


async def fetch_json(session, cookies):
    """Fetch JSON data using bypasser cookies"""
    timestamp = int(time.time() * 10000)
    cache_uuid = uuid4()

    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate, private",
            "Pragma": "no-cache",
            "Cookie": f"cf_clearance:{cookies['cf_clearance']}",
            "cache-timestamp": str(timestamp),
            "cache-uuid": str(cache_uuid),
        }

        url = f"{JSON_URL}?limit=10&cache-timestamp={timestamp}"

        async with session.get(
            url, headers=headers, cookies=cookies, timeout=10
        ) as response:
            if response.status == 200:
                data = await response.json()
                log_message(f"Fetched posts from JSON", "INFO")
                return data, None
            elif response.status == 403:
                log_message(
                    "Cloudflare clearance expired, attempting to refresh", "WARNING"
                )
                cookies = load_cookies(fresh=True)
                if not cookies:
                    raise Exception("CF_CLEARANCE Failed")
                return [], cookies
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return [], None
            else:
                log_message(f"Failed to fetch JSON: HTTP {response.status}", "ERROR")
                return [], None

    except asyncio.TimeoutError:
        log_message(f"Request timeout after 10 seconds", "WARNING")
        return [], None
    except Exception as e:
        if "CF_CLEARANCE Failed" in str(e):
            raise
        log_message(f"Error fetching JSON: {e}", "ERROR")
        return [], None


def is_draft_post(url):
    """Check if the URL is a draft post"""
    return "/publish/post/" in url


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


async def send_to_telegram(post_data):
    current_time = get_current_time()
    post_date = datetime.fromisoformat(post_data["post_date"].replace("Z", "+00:00"))
    post_date_est = post_date.astimezone(pytz.timezone("America/Chicago"))
    update_date = datetime.fromisoformat(post_data["updated_at"].replace("Z", "+00:00"))
    update_date_est = update_date.astimezone(pytz.timezone("America/Chicago"))

    is_draft = is_draft_post(post_data.get("canonical_url", ""))
    title = post_data.get("title", "")
    social_title = post_data.get("social_title", "")

    message = (
        f"<b>{'[DRAFT] ' if is_draft else ''}New J Capital Research Article!</b>\n\n"
    )
    message += (
        f"<b>Published Date:</b> {post_date_est.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    )
    message += (
        f"<b>Updated Date:</b> {update_date_est.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    )
    message += f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Social Title:</b> {social_title}\n"
    message += f"<b>URL:</b> {post_data['canonical_url']}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Article sent to Telegram: {post_data['canonical_url']}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()
    cookies = load_cookies()

    if not cookies:
        log_message("Failed to get valid cf_clearance", "CRITICAL")
        return

    async with aiohttp.ClientSession() as session:
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
                posts, new_cookies = await fetch_json(session, cookies)

                cookies = new_cookies if new_cookies is not None else cookies

                if not posts:
                    log_message("Failed to fetch posts or no posts found", "WARNING")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                new_posts = [
                    post
                    for post in posts
                    if post.get("canonical_url")
                    and post["canonical_url"] not in processed_urls
                ]

                if new_posts:
                    log_message(f"Found {len(new_posts)} new posts to process.", "INFO")

                    for post in new_posts:
                        await send_to_telegram(post)
                        processed_urls.add(post["canonical_url"])

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
