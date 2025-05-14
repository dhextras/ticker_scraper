import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional, Set, Tuple

import aiohttp
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
BASE_URL = "https://jehoshaphatresearch.com/wp-json/wp/v2/posts"
START_ID = 649
CHECK_INTERVAL = 1  # seconds
PROCESSED_IDS_FILE = "data/jehoshaphat_processed_ids.json"
SESSION_FILE = "data/jehoshaphat_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("JEHOSHAPHAT_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("JEHOSHAPHAT_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_cookies(frash=False) -> Optional[Dict[str, Any]]:
    try:
        cookies = None
        if frash == False:
            if not os.path.exists(SESSION_FILE):
                log_message(f"Session file not found: {SESSION_FILE}", "WARNING")
            else:
                with open(SESSION_FILE, "r") as f:
                    cookies = json.load(f)

        # Validate cookies again
        if not cookies or cookies.get("cf_clearance", "") == "":
            log_message(
                "Invalid or missing 'cf_clearance' in cookies. Attempting to regenerate.",
                "WARNING",
            )
            bypass = bypasser(BASE_URL, SESSION_FILE)

            if not bypass or bypass == False:
                return

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


def load_processed_ids() -> Set[int]:
    try:
        with open(PROCESSED_IDS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_ids(ids: Set[int]) -> None:
    with open(PROCESSED_IDS_FILE, "w") as f:
        json.dump(list(ids), f, indent=2)
    log_message("Processed IDs saved.", "INFO")


async def fetch_post_by_id(
    session, post_id: int, cookies: Dict[str, Any]
) -> Tuple[Optional[Dict], Optional[Dict]]:
    url = f"{BASE_URL}/{post_id}"
    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "Cache-Control": "max-age=0",
            "Cookie": f"cf_clearance:{cookies['cf_clearance']}",
        }

        async with session.get(url, headers=headers, cookies=cookies) as response:
            if response.status == 200:
                data = await response.json()
                log_message(f"Successfully fetched post ID {post_id}", "INFO")
                return data, None
            elif response.status == 403:
                log_message(
                    "Cloudflare clearance expired, refreshing cookies...", "WARNING"
                )
                cookies = load_cookies(frash=True)
                if not cookies:
                    raise Exception("CF_CLEARANCE Failed: Post")
                return None, cookies
            elif response.status == 404:
                log_message(f"Post ID {post_id} not found", "INFO")
                return None, None
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return None, None
            else:
                log_message(
                    f"Failed to fetch post {post_id}: HTTP {response.status}", "ERROR"
                )
                return None, None
    except Exception as e:
        if "CF_CLEARANCE Failed" in str(e):
            raise
        log_message(f"Error fetching post {post_id}: {e}", "ERROR")
        return None, None


async def send_post_to_telegram(post_data: Dict) -> None:
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    # Extract ticker from title if possible
    title = post_data.get("title", {}).get("rendered", "Unknown Title")

    import re

    ticker_match = re.search(r"\(([A-Z]+)\)", title)
    ticker = ticker_match.group(1) if ticker_match else "Unknown"

    post_id = post_data.get("id", "Unknown ID")
    post_date = post_data.get("date", "Unknown Date")
    post_url = post_data.get("link", "No Post Link")

    message = f"<b>New Jehoshaphat Research Media - Post ID</b>\n\n"
    message += f"<b>Time Found:</b> {timestamp}\n"
    message += f"<b>Post ID:</b> {post_id}\n"
    message += f"<b>Post Date:</b> {post_date}\n"
    message += f"<b>Title:</b> {title}\n"

    if ticker != "Unknown":
        message += f"<b>Possible Ticker:</b> {ticker}\n"

    message += f"<b>Post URL:</b> {post_url}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Post ID {post_id} sent to Telegram", "INFO")


async def run_scraper() -> None:
    processed_ids = load_processed_ids()
    cookies = load_cookies()

    if not cookies:
        log_message("Failed to get valid cf_clearance", "CRITICAL")
        return

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()

            log_message(
                "Market is open. Starting to check for new posts by ID...", "DEBUG"
            )
            _, _, market_close_time = get_next_market_times()

            current_id = START_ID

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                if current_id in processed_ids:
                    current_id += 1
                    continue

                log_message(f"Checking post ID: {current_id}")
                post_data, pos_cookies = await fetch_post_by_id(
                    session, current_id, cookies
                )

                # Update cookies if needed
                cookies = pos_cookies if pos_cookies is not None else cookies

                if post_data:
                    await send_post_to_telegram(post_data)
                    processed_ids.add(current_id)
                    save_processed_ids(processed_ids)

                    current_id += 1
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
