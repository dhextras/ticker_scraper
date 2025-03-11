import asyncio
import json
import os
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple
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
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
SEARCH_URL = "https://oxfordclub.com/wp-json/wp/v2/search"
MEDIA_URL = "https://oxfordclub.com/wp-json/wp/v2/media"
LOGIN_URL = "https://oxfordclub.com/wp-login.php"
USERNAME = os.getenv("OXFORDCLUB_USERNAME")
PASSWORD = os.getenv("OXFORDCLUB_PASSWORD")
CHECK_INTERVAL = 0.3  # seconds between batch checks
BATCH_SIZE = 40  # Number of IDs to check in one batch
LATEST_ID_FILE = "data/oxfordclub_search_latest_id.json"
TELEGRAM_BOT_TOKEN = os.getenv("OXFORDCLUB_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("OXFORDCLUB_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
STARTING_ID = 133350  # A bit higher than the highest in the known list
PROXY_FILE = "cred/proxies.json"
PAGE_PROCESS_CONCURRENT_REQUESTS = (
    5  # Number of concurrent ( with proxy ) requests to process a page
)

os.makedirs("data", exist_ok=True)
os.makedirs("cred", exist_ok=True)

# Global variables for proxy management
active_proxies: Set[str] = set()
proxy_lock = asyncio.Lock()


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


def load_proxies() -> List[str]:
    """Load proxies from json file"""
    try:
        with open(PROXY_FILE, "r") as f:
            data = json.load(f)
            return data.get("oxford_tradesmith", [])
    except Exception as e:
        log_message(f"Error loading proxies: {e}", "ERROR")
        return []


async def get_available_proxy(proxies: List[str]) -> str:
    """Get a random available proxy that isn't currently in use"""
    async with proxy_lock:
        available_proxies = set(proxies) - active_proxies
        if not available_proxies:
            log_message(
                "No available proxies, waiting for one to be released", "WARNING"
            )
            # Release the lock while waiting
            await asyncio.sleep(0.5)
            return await get_available_proxy(proxies)

        proxy = random.choice(list(available_proxies))
        active_proxies.add(proxy)
        return proxy


async def release_proxy(proxy: str) -> None:
    """Release a proxy back to the available pool"""
    async with proxy_lock:
        active_proxies.discard(proxy)


async def fetch_with_proxy(
    session: requests.Session,
    url: str,
    proxy: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> Optional[requests.Response]:
    """Make a request using a proxy"""
    response = None
    try:
        proxies = {
            "http": proxy,
            "https": proxy,
        }

        response = await asyncio.to_thread(
            session.get,
            url,
            proxies=proxies,
            headers=headers if headers else get_headers(),
            timeout=timeout,
        )
        await release_proxy(proxy)
        return response
    except requests.Timeout:
        await release_proxy(proxy)
        log_message(
            f"Took more then {timeout} sec to fetch {url} with proxy: {proxy}",
            "WARNING",
        )
        return response
    except Exception as e:
        await release_proxy(proxy)
        log_message(f"Error with proxy {proxy}: {e}", "ERROR")
        return response


async def fetch_without_proxy(
    session: requests.Session,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> Optional[requests.Response]:
    """Make a request without using a proxy"""
    try:
        response = await asyncio.to_thread(
            session.get,
            url,
            headers=headers if headers else get_headers(),
            timeout=timeout,
        )
        return response
    except requests.Timeout:
        log_message(
            f"Took more then {timeout} sec to fetch {url} without proxy",
            "WARNING",
        )
        return None
    except Exception as e:
        log_message(f"Error without proxy: {e}", "ERROR")
        return None


async def check_post_by_search(
    session: requests.Session, post_id: int, proxies: List[str]
) -> Optional[List[Dict[str, Any]]]:
    """
    Check if a post with the given ID exists by querying the Search API
    """
    try:
        start_time = time.time()
        url = f"{SEARCH_URL}?include={post_id}"
        proxy = await get_available_proxy(proxies)
        response = await fetch_with_proxy(session, url, proxy, timeout=3)
        time_to_fetch = time.time() - start_time

        if response and response.status_code == 200:
            data = response.json()
            if data and isinstance(data, list) and len(data) > 0:
                log_message(f"Found post with ID: {post_id}", "INFO")
                found_post = data[0]
                if found_post and found_post.get("url"):
                    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")

                    url = found_post.get("url", "")
                    await process_page_multi(session, url, proxies)

                    await send_post_to_telegram(found_post, timestamp, time_to_fetch)

                return data

            else:
                return None
        else:
            status_code = response.status_code if response else None
            if status_code:
                log_message(
                    f"Failed to check post ID {post_id}: HTTP {status_code}",
                    "ERROR",
                )
            return None
    except Exception as e:
        log_message(f"Error checking post ID {post_id}: {e}", "ERROR")
        return None


async def fetch_latest_media_id(
    session: requests.Session, proxies: List[str]
) -> Optional[int]:
    """
    Fetch the latest media ID from the WordPress API
    """
    try:
        url = f"{MEDIA_URL}?per_page=1&orderby=id&order=desc"
        proxy = await get_available_proxy(proxies)
        response = await fetch_with_proxy(session, url, proxy, timeout=5)

        if response and response.status_code == 200:
            data = response.json()
            if data and isinstance(data, list) and len(data) > 0:
                media_id = data[0].get("id")
                log_message(f"Latest media ID found: {media_id}", "INFO")
                return media_id
            else:
                log_message("No media items found", "WARNING")
                return None
        else:
            status_code = response.status_code if response else None
            if status_code:
                log_message(
                    f"Failed to fetch latest media ID: HTTP {status_code}",
                    "ERROR",
                )
            return None
    except Exception as e:
        log_message(f"Error fetching latest media ID: {e}", "ERROR")
        return None


async def process_page(
    session: requests.Session, url: str, proxy: str
) -> Optional[Tuple[str, str, str, float]]:
    try:
        start_time = time.time()
        response = await fetch_with_proxy(session, url, proxy)
        total_seconds = time.time() - start_time

        if response and response.status_code == 200:
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
                    r"(NYSE|NASDAQ):\s*(\w+)", section, re.IGNORECASE
                )

                if (
                    sell_match
                    and ticker_match
                    and sell_match.start() < ticker_match.start()
                ):
                    exchange, ticker = ticker_match.groups()
                    return (ticker, exchange, "Sell", total_seconds)
                elif (
                    buy_match
                    and ticker_match
                    and buy_match.start() < ticker_match.start()
                ):
                    exchange, ticker = ticker_match.groups()
                    return (ticker, exchange, "Buy", total_seconds)
                elif not ticker_match:
                    log_message(f"No ticker found in section: {url}", "WARNING")
                elif not buy_match or (
                    buy_match
                    and ticker_match
                    and buy_match.start() > ticker_match.start()
                ):
                    log_message(
                        f"'Buy' not found before ticker in section: {url}", "WARNING"
                    )

            log_message(
                f"Took {total_seconds:.2f}s to fetch and process URL: {url}", "WARNING"
            )
        else:
            status_code = response.status_code if response else None
            if status_code:
                log_message(f"Failed to fetch page: HTTP {status_code}", "ERROR")
    except Exception as e:
        log_message(f"Error processing page {url}: {e}", "ERROR")

    return None


async def process_page_without_proxy(
    session: requests.Session, url: str
) -> Optional[Tuple[str, str, str, float]]:
    """Process page without using any proxy"""
    try:
        start_time = time.time()
        response = await fetch_without_proxy(session, url)
        total_seconds = time.time() - start_time

        if response and response.status_code == 200:
            content = response.text
            soup = BeautifulSoup(content, "html.parser")
            all_text = soup.get_text(separator=" ", strip=True)

            action_sections = re.split(r"Action to Take", all_text, flags=re.IGNORECASE)

            if len(action_sections) < 2:
                log_message(f"'Action to Take' not found (no proxy): {url}", "WARNING")

            for section in action_sections[1:]:
                buy_match = re.search(r"Buy", section, re.IGNORECASE)
                sell_match = re.search(r"Sell", section, re.IGNORECASE)
                ticker_match = re.search(
                    r"(NYSE|NASDAQ):\s*(\w+)", section, re.IGNORECASE
                )

                if (
                    sell_match
                    and ticker_match
                    and sell_match.start() < ticker_match.start()
                ):
                    exchange, ticker = ticker_match.groups()
                    return (ticker, exchange, "Sell", total_seconds)
                elif (
                    buy_match
                    and ticker_match
                    and buy_match.start() < ticker_match.start()
                ):
                    exchange, ticker = ticker_match.groups()
                    return (ticker, exchange, "Buy", total_seconds)

        return None
    except Exception as e:
        log_message(f"Error processing page without proxy {url}: {e}", "ERROR")
        return None


async def process_page_multi(
    session: requests.Session, url: str, proxies: List[str]
) -> None:
    """Process page with multiple concurrent requests (with and without proxies)"""
    # Create tasks for processing with multiple proxies + one without proxy
    tasks = []
    tasks.append(process_page_without_proxy(session, url))

    for _ in range(PAGE_PROCESS_CONCURRENT_REQUESTS):
        proxy = await get_available_proxy(proxies)
        tasks.append(process_page(session, url, proxy))

    # Create a future for the first completed task
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # Process the first completed task
    first_result = None
    for task in done:
        result = task.result()
        if result:
            first_result = result
            break

    for task in pending:
        task.cancel()

    # If we got a result, send it to Telegram
    if first_result:
        ticker, exchange, action, total_seconds = first_result
        timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")
        await send_match_to_telegram(
            url, ticker, exchange, action, timestamp, total_seconds
        )
    else:
        log_message(f"No actionable content found in page: {url}", "WARNING")


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
    # await send_ws_message(
    #     {
    #         "name": "Oxford Club - Search ID",
    #         "type": action,
    #         "ticker": ticker,
    #         "sender": "oxfordclub",
    #     },
    #     WS_SERVER_URL,
    # )

    message = f"<b>New Stock Match Found - Search ID</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Stock Symbol:</b> {exchange}:{ticker}\n"
    message += f"<b>Article Fetch time:</b> {total_seconds:.2f}s\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"Match sent to Telegram and WebSocket: {exchange}:{ticker} - {url}", "INFO"
    )


async def check_batch_of_posts(
    session: requests.Session, latest_id: int, batch_size: int, proxies: List[str]
) -> int:
    """Check a batch of post IDs concurrently using multiple proxies"""
    tasks = []

    for offset in range(batch_size):
        current_id = latest_id + offset + 1
        tasks.append(check_post_by_search(session, current_id, proxies))

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

    proxies = load_proxies()
    if not proxies:
        log_message("No proxies available. Exiting...", "CRITICAL")
        return

    session = requests.Session()
    if not login_sync(session):
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

            log_message(f"Checking for new posts from ID: {latest_id + 1}", "INFO")

            # Check a batch of posts concurrently
            found_id = await check_batch_of_posts(
                session, latest_id, BATCH_SIZE, proxies
            )

            # Fetch the latest media ID and compare the highest ID
            latest_media_id = await fetch_latest_media_id(session, proxies)
            if latest_media_id and latest_media_id > found_id:
                log_message(
                    f"Latest media ID {latest_media_id} is higher than found post ID {found_id}",
                    "INFO",
                )
                found_id = latest_media_id

            if found_id > latest_id:
                latest_id = found_id
                save_latest_id(latest_id)
            else:
                log_message(f"No new posts found", "INFO")

            await asyncio.sleep(CHECK_INTERVAL)


def main() -> None:
    if not all([USERNAME, PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP, WS_SERVER_URL]):
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
