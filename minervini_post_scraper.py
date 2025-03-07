import asyncio
import json
import os
import random
import time
import uuid
from datetime import datetime
from typing import Dict, Optional

import aiohttp
import pytz
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
TELEGRAM_BOT_TOKEN = os.getenv("MINERVINI_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("MINERVINI_TELEGRAM_GRP")
TOKENS_FILE = "data/minervini_access_token.json"
BASE_URL = "https://mpa.minervini.com/api/streams/1/posts/"
PROCESSED_IDS_FILE = "data/minervini_processed_post_ids.json"

os.makedirs("data", exist_ok=True)


def load_processed_ids() -> set:
    try:
        with open(PROCESSED_IDS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_ids(processed_ids: set) -> None:
    with open(PROCESSED_IDS_FILE, "w") as f:
        json.dump(list(processed_ids), f)
    log_message("Processed IDs saved", "INFO")


def load_tokens() -> Optional[Dict]:
    try:
        with open(TOKENS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        log_message(f"Tokens file not found: {TOKENS_FILE}", "ERROR")
        return None


def get_random_headers():
    """Generate random headers for requests"""
    return {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": str(uuid.uuid4()),
        "X-Request-Time": str(int(time.time())),
    }


def get_random_cache_buster():
    """Generate random cache busting url variable for requests"""
    cache_busters = [
        ("cache_timestamp", lambda: int(time.time() * 10000)),
        ("request_uuid", lambda: str(uuid.uuid4())),
        ("cache_time", lambda: int(time.time())),
        ("ran_time", lambda: int(time.time() * 1000)),
        ("no_cache_uuid", lambda: str(uuid.uuid4().hex[:16])),
        ("unique", lambda: f"{int(time.time())}-{random.randint(1000, 9999)}"),
        ("req_uuid", lambda: f"req-{uuid.uuid4().hex[:8]}"),
        ("tist", lambda: str(int(time.time()))),
    ]

    variable, value_generator = random.choice(cache_busters)
    return variable, value_generator()


def format_time(time_str: str) -> str:
    dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    ny_time = dt.astimezone(pytz.timezone("America/Chicago"))
    return ny_time.strftime("%Y-%m-%d %H:%M:%S EDT")


async def send_alert(msg: str):
    alert = f"🚨 ALERT: {msg}\nPlease check the server immediately!"
    await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def process_post(post: Dict) -> str:
    current_time = get_current_time()
    soup = BeautifulSoup(post["content"], "html.parser")
    formatted_content = soup.get_text(separator="\n", strip=True)

    message = f"<b>Minervini Post Update</b>\n\n"
    message += (
        f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S EDT')}\n"
    )
    message += f"<b>Post Time:</b> {format_time(post['published'])}\n"
    message += f"<b>Title:</b> {post['title']}\n"
    message += f"<b>Color:</b> {post['color']}\n\n"
    message += f"<b>Content:</b>\n\n{formatted_content[:500]}"

    return message


async def check_minervini_posts(session: aiohttp.ClientSession) -> None:
    tokens = load_tokens()
    if not tokens:
        log_message("Token isn't available...", "WARNING")
        return

    log_message("Fetching for new posts...", "INFO")

    processed_ids = load_processed_ids()
    random_headers = get_random_headers()
    random_variable, random_value = get_random_cache_buster()

    current_date = get_current_time()
    params = {"date": current_date.strftime("%Y-%m-%d"), random_variable: random_value}

    headers = {
        **random_headers,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "x-csrftoken": tokens["csrftoken"],
    }

    cookies = {"csrftoken": tokens["csrftoken"], "sessionid": tokens["sessionid"]}

    try:
        async with session.get(
            BASE_URL,
            params=params,
            headers=headers,
            cookies=cookies,
        ) as response:
            if 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return
            elif response.status != 200:
                await send_alert(f"Unexpected status code: {response.status}")
                log_message(f"Unexpected response: {await response.text()}", "ERROR")
                return

            data = await response.json()

            if not isinstance(data, dict) or "results" not in data:
                await send_alert("Invalid response format received")
                log_message(f"Invalid response: {json.dumps(data)}", "ERROR")
                return

            if data["results"]:
                new_posts = [
                    post
                    for post in data["results"]
                    if str(post["id"]) not in processed_ids
                ]
                if new_posts:
                    log_message(f"Fetched {len(new_posts)} new posts", "INFO")
                    for post in new_posts:
                        message = await process_post(post)
                        await send_telegram_message(
                            message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP
                        )
                        processed_ids.add(str(post["id"]))
                    save_processed_ids(processed_ids)
            else:
                log_message("No posts found for current date", "INFO")

    except Exception as e:
        await send_alert(f"Error occurred: {str(e)[:200]}")
        log_message(f"Error in check_minervini_posts: {e}", "ERROR")


async def run_scraper():
    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open(start=8, end=15)
            log_message("Market is open. Starting to check for posts...", "DEBUG")
            _, _, market_close_time = get_next_market_times(start=8, end=15)

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                await check_minervini_posts(session)
                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        return

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")


if __name__ == "__main__":
    main()
