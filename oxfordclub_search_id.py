import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import requests
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
SEARCH_URL = "https://oxfordclub.com/wp-json/wp/v2/search"
LOGIN_URL = "https://oxfordclub.com/wp-login.php"
USERNAME = os.getenv("OXFORDCLUB_USERNAME")
PASSWORD = os.getenv("OXFORDCLUB_PASSWORD")
CHECK_INTERVAL = 0.3  # seconds between batch checks
BATCH_SIZE = 40  # Number of IDs to check in one batch
LATEST_ID_FILE = "data/oxfordclub_search_latest_id.json"
TELEGRAM_BOT_TOKEN = os.getenv("OXFORDCLUB_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("OXFORDCLUB_TELEGRAM_GRP")
STARTING_ID = 133350  # A bit higher than the highest in the known list

os.makedirs("data", exist_ok=True)


def load_latest_id() -> Optional[int]:
    try:
        with open(LATEST_ID_FILE, "r") as f:
            data = json.load(f)
            return data.get("latest_id", STARTING_ID)
    except FileNotFoundError:
        save_latest_id(STARTING_ID)
        return STARTING_ID


def save_latest_id(latest_id: int) -> None:
    with open(LATEST_ID_FILE, "w") as f:
        json.dump({"latest_id": latest_id}, f, indent=2)
    log_message(f"Latest ID saved: {latest_id}", "INFO")


def login_sync(session: requests.Session) -> bool:
    try:
        payload = {"log": USERNAME, "pwd": PASSWORD}
        response = session.post(LOGIN_URL, data=payload)
        if response.status_code == 200:
            log_message("Login successful", "INFO")
            return True
        else:
            log_message(f"Login failed: HTTP {response.status_code}", "ERROR")
            return False
    except Exception as e:
        log_message(f"Error during login: {e}", "ERROR")
        return False


def get_headers() -> Dict[str, str]:
    timestamp = int(time.time() * 10000)
    cache_uuid = uuid4()

    return {
        "Connection": "keep-alive",
        "cache-control": "no-cache, no-store, max-age=0, must-revalidate, private",
        "pragma": "no-cache",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36",
        "cache-timestamp": str(timestamp),
        "cache-uuid": str(cache_uuid),
    }


async def check_post_by_id(
    session: requests.Session, post_id: int
) -> Optional[List[Dict[str, Any]]]:
    """
    Check if a post with the given ID exists by querying the Posts API
    """
    try:
        start_time = time.time()
        url = f"{SEARCH_URL}?include={post_id}"
        response = await asyncio.to_thread(
            session.get, url, headers=get_headers(), timeout=15
        )
        time_to_fetch = time.time() - start_time

        if response.status_code == 200:
            data = response.json()
            if data and isinstance(data, list) and len(data) > 0:
                log_message(f"Found post with ID: {post_id}", "INFO")
                found_post = data[0]
                if found_post and found_post.get("url"):
                    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")

                    url = found_post.get("url", "")
                    await process_page(session, url)

                    await send_post_to_telegram(found_post, timestamp, time_to_fetch)

                return data

            else:
                return None
        else:
            log_message(
                f"Failed to check post ID {post_id}: HTTP {response.status_code}",
                "ERROR",
            )
            return None
    except Exception as e:
        log_message(f"Error checking post ID {post_id}: {e}", "ERROR")
        return None


async def process_page(
    session: requests.Session, url: str
) -> Optional[Tuple[str, str, str, float]]:
    try:
        start_time = time.time()
        response = await asyncio.to_thread(
            session.get, url, headers=get_headers(), timeout=15
        )
        total_seconds = time.time() - start_time

        if response.status_code == 200:
            content = response.text
            soup = BeautifulSoup(content, "html.parser")
            all_text = soup.get_text(separator=" ", strip=True)

            action_sections = re.split(r"Action to Take", all_text, flags=re.IGNORECASE)

            if len(action_sections) < 2:
                log_message(f"'Action to Take' not found: {url}", "WARNING")

            for section in action_sections[1:]:
                buy_match = re.search(r"Buy", section, re.IGNORECASE)
                sell_match = re.search(r"Sell", section, re.IGNORECASE)
                ticker_match = re.search(
                    r"(?:NYSE|NASDAQ)\s*:\s*\(?\*?([A-Z]{1,5})\*?\)?",
                    section,
                    re.IGNORECASE,
                )

                exchange: str = ""
                ticker: str = ""

                if ticker_match:
                    exchange = ticker_match.group(1)
                    ticker = ticker_match.group(2)
                else:
                    # fallback: ticker only, no exchange
                    ticker_match = re.search(
                        r"\(\s*([A-Z]{1,5})\s*\)",
                        section,
                        re.IGNORECASE,
                    )
                    if ticker_match:
                        ticker = ticker_match.group(1)

                if ticker:
                    if (
                        sell_match
                        and ticker_match
                        and sell_match.start() < ticker_match.start()
                    ):
                        timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")
                        await send_match_to_telegram(
                            url, ticker, exchange, "Sell", timestamp, total_seconds
                        )
                        return (ticker, exchange, "Sell", total_seconds)

                    elif (
                        buy_match
                        and ticker_match
                        and buy_match.start() < ticker_match.start()
                    ):
                        timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")
                        await send_match_to_telegram(
                            url, ticker, exchange, "Buy", timestamp, total_seconds
                        )
                        return (ticker, exchange, "Buy", total_seconds)

            log_message(
                f"Took {total_seconds:.2f}s to fetch and process URL: {url}", "WARNING"
            )
        else:
            log_message(f"Failed to fetch page: HTTP {response.status_code}", "ERROR")
    except Exception as e:
        log_message(f"Error processing page {url}: {e}", "ERROR")

    return None


async def send_post_to_telegram(
    post_details: Dict[str, Any], timestamp: str, time_to_fetch: float
) -> None:
    message = f"<b>New Post Found - Search Id</b>\n\n"
    message += f"<b>Id:</b> {post_details.get('id', '-')}\n"
    message += f"<b>Url:</b> {post_details.get('url', '-')}\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>time_to_fetch:</b> {time_to_fetch:.2f}s"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def send_match_to_telegram(
    url: str,
    ticker: str,
    exchange: str,
    action: str,
    timestamp: str,
    total_seconds: float,
) -> None:

    await send_ws_message(
        {
            "name": "Oxford Club - Search ID",
            "type": action,
            "ticker": ticker,
            "sender": "oxfordclub",
        },
    )

    message = f"<b>New Stock Match Found - Search ID</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Stock Symbol:</b> {exchange}:{ticker}\n"
    message += f"<b>Article Fetch time:</b> {total_seconds:.2f}s\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Match sent to Telegram: {exchange}:{ticker} - {url}", "INFO")


async def check_batch_of_posts(
    session: requests.Session, latest_id: int, batch_size: int
) -> int:
    """Check a batch of post IDs concurrently"""
    tasks = []

    for offset in range(batch_size):
        current_id = latest_id + offset + 1
        tasks.append(check_post_by_id(session, current_id))

    results = await asyncio.gather(*tasks)

    # Find the highest found_id and return it
    found_id = latest_id
    for offset, result in enumerate(results):
        if result and isinstance(result, list) and len(result) > 0:
            current_id = latest_id + offset + 1
            found_id = found_id if found_id > current_id else current_id

    return found_id


async def run_scraper() -> None:
    latest_id = load_latest_id()

    if not latest_id:
        latest_id = STARTING_ID
        save_latest_id(latest_id)

    session = requests.Session()
    if not login_sync(session):
        return

    while True:
        await sleep_until_market_open()
        await initialize_websocket()

        log_message("Market is open. Starting to check for new posts...", "DEBUG")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()

            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                break

            log_message(f"Checking for new posts from ID: {latest_id + 1}", "INFO")

            # Check a batch of posts concurrently
            found_id = await check_batch_of_posts(session, latest_id, BATCH_SIZE)

            if found_id > latest_id:
                latest_id = found_id
                save_latest_id(latest_id)
            else:
                log_message(f"No new posts found", "INFO")

            await asyncio.sleep(CHECK_INTERVAL)


def main() -> None:
    if not all([USERNAME, PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
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
