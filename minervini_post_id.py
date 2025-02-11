import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime

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

load_dotenv()

# Constants
CHECK_INTERVAL = 1
DEFAULT_STARTING_ID = 35300  # Default starting ID - change it later if needed
LAST_ID_FILE = "data/minervini_last_processed_id.json"
TOKENS_FILE = "data/minervini_access_token.json"
BASE_URL = "https://mpa.minervini.com/api/streams/1/posts/"
TELEGRAM_BOT_TOKEN = os.getenv("MINERVINI_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("MINERVINI_TELEGRAM_GRP")
MAX_EMPTY_CHECKS = 10
LOOKAHEAD_IDS = 1

os.makedirs("data", exist_ok=True)


def load_tokens():
    try:
        with open(TOKENS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log_message(f"Error loading tokens: {e}", "ERROR")
        return None


def load_last_id():
    try:
        with open(LAST_ID_FILE, "r") as f:
            data = json.load(f)
            return data.get("last_id", DEFAULT_STARTING_ID) + 1
    except FileNotFoundError:
        return DEFAULT_STARTING_ID + 1


def save_last_id(last_id):
    with open(LAST_ID_FILE, "w") as f:
        json.dump({"last_id": last_id}, f, indent=2)
    log_message(f"Last processed ID saved: {last_id}", "INFO")


async def send_alert(msg: str):
    alert = f"🚨 ALERT: {msg}\nPlease check the server immediately!"
    await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def send_message(msg: str):
    alert = f"🚨 MESSAGE: {msg}"
    await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def fetch_post(session, tokens, post_id):
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": str(uuid.uuid4()),
        "X-Request-Time": str(int(time.time())),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "x-csrftoken": tokens["csrftoken"],
    }

    cookies = {"csrftoken": tokens["csrftoken"], "sessionid": tokens["sessionid"]}

    try:
        async with session.get(
            f"{BASE_URL}{post_id}/",
            cookies=cookies,
            headers=headers,
        ) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 404:
                return None

            await send_alert(f"Unexpected status code: {response.status}")
            log_message(f"Failed to fetch post: HTTP {response.status}", "ERROR")
            return None
    except Exception as e:
        log_message(f"Error fetching post: {e}", "ERROR")
        await send_alert(f"Error occurred: {str(e)[:200]}")
        return None


async def send_post_alert(post):
    current_time = get_current_time()
    post_time = datetime.fromisoformat(post["published"].replace("Z", "+00:00"))

    soup = BeautifulSoup(post["content"], "html.parser")
    formatted_content = soup.get_text(separator="\n", strip=True)

    message = f"<b>New Minervini Post Alert - ID!</b>\n\n"
    message += f"<b>ID:</b> {post['id']}\n"
    message += f"<b>Title:</b> {post['title']}\n"
    message += f"<b>Color:</b> {post['color']}\n"
    message += f"<b>Post Time:</b> {post_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += (
        f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
    )
    message += f"<b>Content:</b>\n\n{formatted_content[:500]}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Post alert sent to Telegram: ID {post['id']}", "INFO")


async def check_lookahead_ids(session, tokens, current_id):
    for offset in range(1, LOOKAHEAD_IDS + 1):
        lookahead_id = current_id + offset
        post_data = await fetch_post(session, tokens, lookahead_id)

        if post_data:
            log_message(f"Found post at lookahead ID: {lookahead_id}", "INFO")
            await send_message(
                f"Found post at lookahead ID: {lookahead_id}, thus skipping id: {current_id}\nLater check too see if this id get used or not\n{BASE_URL}{current_id}/"
            )
            return lookahead_id

    return None


async def run_scraper():
    tokens = load_tokens()
    if not tokens:
        log_message("Failed to load tokens", "CRITICAL")
        return

    current_id = load_last_id()
    empty_checks = 0

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open(start=8, end=15)
            log_message("Market is open. Starting to check for new posts...", "DEBUG")
            _, _, market_close_time = get_next_market_times(start=8, end=15)

            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                post_data = await fetch_post(session, tokens, current_id)

                if post_data:
                    await send_post_alert(post_data)
                    save_last_id(current_id)
                    current_id += 1
                    empty_checks = 0
                    log_message(f"Moving to next ID: {current_id}", "INFO")
                else:
                    empty_checks += 1
                    log_message(
                        f"No post found for ID: {current_id}, empty check count: {empty_checks}",
                        "INFO",
                    )

                    if empty_checks >= MAX_EMPTY_CHECKS:
                        log_message(
                            "Maximum empty checks reached, checking lookahead IDs",
                            "INFO",
                        )
                        new_id = await check_lookahead_ids(session, tokens, current_id)

                        if new_id:
                            current_id = new_id
                            empty_checks = 0
                            log_message(
                                f"Switching to new ID found in lookahead: {current_id}",
                                "INFO",
                            )
                        else:
                            empty_checks = 0
                            log_message(
                                "No posts found in lookahead, resetting empty checks",
                                "INFO",
                            )

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
