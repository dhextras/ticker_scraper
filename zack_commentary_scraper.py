import asyncio
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from time import time
from typing import Dict, NamedTuple, Set

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open

load_dotenv()


class ZacksService(NamedTuple):
    name: str
    url: str


# Constants
TELEGRAM_BOT_TOKEN = os.getenv("ZACKS_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("ZACKS_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
ZACKS_USERNAME = os.getenv("ZACKS_USERNAME")
ZACKS_PASSWORD = os.getenv("ZACKS_PASSWORD")
CHECK_INTERVAL = 0.2  # seconds
STARTING_CID = 43250  # If in the future you had to run the script for the first time chooose the recent id

DATA_DIR = Path("data")
CRED_DIR = Path("cred")
PROXY_FILE = CRED_DIR / "zacks_commentary_proxies.json"
COMMENT_ID_FILE = DATA_DIR / "zacks_last_comment_id.json"

ZACKS_SERVICES = [
    # Investor Services
    ZacksService("Investor Collection", "investorcollection"),
    ZacksService("ETF Investor", "etfinvestor"),
    ZacksService("Home Run Investor", "homerun"),
    ZacksService("Income Investor", "incomeinvestor"),
    ZacksService("Stocks Under $10", "stocksunder10"),
    ZacksService("Value Investor", "valueinvestor"),
    ZacksService("Zacks Top 10", "top10"),
    # Innovators
    ZacksService("Alternative Energy Innovators", "alternativeenergyinnovators"),
    ZacksService("Blockchain Innovators", "blockchaininnovators"),
    ZacksService("Commodity Innovators", "commodityinnovators"),
    ZacksService("Healthcare Innovators", "healthcareinnovators"),
    ZacksService("Marijuana Innovators", "marijuanainnovators"),
    ZacksService("Technology Innovators", "technologyinnovators"),
    # Trading Services
    ZacksService("Black Box Trader", "blackboxtrader"),
    ZacksService("Counterstrike", "counterstrike"),
    ZacksService("Headline Trader", "headlinetrader"),
    ZacksService("Insider Trader", "insidertrader"),
    ZacksService("Large-Cap Trader", "largecaptrader"),
    ZacksService("Options Trader", "optionstrader"),
    ZacksService("Short Sell List", "shortlist"),
    ZacksService("TAZR", "tazr"),
    # Other Services
    ZacksService("Zacks Confidential", "confidential"),
    ZacksService("Zacks Premium", "premium"),
    ZacksService("Zacks Ultimate", "ultimate"),
]

# Global variables
alert_locks: Dict[str, asyncio.Lock] = {
    service.name: asyncio.Lock() for service in ZACKS_SERVICES
}
proxy_lock = asyncio.Lock()
session_lock = asyncio.Lock()
comment_id_lock = asyncio.Lock()
active_proxies: Set[str] = set()
proxy_sessions: Dict[str, aiohttp.ClientSession] = {}
current_comment_id = None


def load_last_comment_id():
    """Load the last processed comment ID from file"""
    try:
        if COMMENT_ID_FILE.exists():
            with open(COMMENT_ID_FILE, "r") as f:
                data = json.load(f)
                return data.get("last_comment_id", STARTING_CID)
        return STARTING_CID
    except Exception as e:
        log_message(f"Error loading last comment ID: {e}", "ERROR")
        return STARTING_CID


async def save_comment_id(comment_id: int):
    """Save the last processed comment ID"""
    async with comment_id_lock:
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(COMMENT_ID_FILE, "w") as f:
                json.dump({"last_comment_id": comment_id}, f)
        except Exception as e:
            log_message(f"Error saving comment ID: {e}", "ERROR")


def load_proxies():
    """Load proxies from json file"""
    try:
        with open(PROXY_FILE, "r") as f:
            data = json.load(f)
            return data["proxies"]
    except Exception as e:
        log_message(f"Error loading proxies: {e}", "ERROR")
        return []


async def get_available_proxy(proxies):
    """Get a random available proxy that isn't currently in use"""
    async with proxy_lock:
        available_proxies = set(proxies) - active_proxies
        if not available_proxies:
            await asyncio.sleep(0.5)
            return await get_available_proxy(proxies)

        proxy = random.choice(list(available_proxies))
        active_proxies.add(proxy)
        return proxy


async def release_proxy(proxy):
    """Release a proxy back to the available pool"""
    proxy = proxy[7:]  # Remove the http:// in the proxy
    async with proxy_lock:
        active_proxies.discard(proxy)


async def create_session(proxy):
    """Create and return a new session with login using specific proxy"""
    try:
        session = aiohttp.ClientSession()

        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.zacks.com",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0",
        }

        data = {
            "force_login": "true",
            "username": ZACKS_USERNAME,
            "password": ZACKS_PASSWORD,
            "remember_me": "on",
        }

        try:
            async with session.post(
                "https://www.zacks.com/tradingservices/index.php",
                headers=headers,
                data=data,
                proxy=proxy,
            ) as response:
                if response.status != 200:
                    await session.close()
                    return None
        except Exception as e:
            await session.close()
            return None

        return session
    except Exception as e:
        log_message(f"Error creating session with proxy {proxy}: {e}", "ERROR")
        return None


async def get_or_create_session(proxy):
    """Get existing session for proxy or create new one"""
    async with session_lock:
        if proxy in proxy_sessions and not proxy_sessions[proxy].closed:
            return proxy_sessions[proxy]

        session = await create_session(proxy)
        if session:
            proxy_sessions[proxy] = session
        return session


async def fetch_commentary(service: ZacksService, comment_id: int, proxy: str):
    """Fetch commentary for a specific service and comment ID"""
    session = await get_or_create_session(proxy)
    if not session:
        return None

    url = f"https://www.zacks.com/{service.url}/commentary.php?cid={comment_id}"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0",
    }

    try:
        async with session.get(url, headers=headers, proxy=proxy) as response:
            await release_proxy(proxy)

            if response.status != 200:
                if response.status in [401, 403]:
                    async with session_lock:
                        if proxy in proxy_sessions:
                            await proxy_sessions[proxy].close()
                            del proxy_sessions[proxy]
                return None
            return await response.text()
    except Exception as e:
        log_message(f"Error fetching commentary for {service.name}: {e}", "ERROR")
        return None


def process_commentary(html: str, service: ZacksService):
    """Extract title and content from commentary HTML"""
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Find the title and content
        title_elem = soup.select_one("#cdate-most-recent > article > header > div > h4")
        content_elem = soup.select_one("#cdate-most-recent > article > section")

        if not title_elem or not content_elem:
            return None

        title = title_elem.get_text(strip=True)
        content = content_elem.get_text(strip=True)

        if not title or not content:
            return None

        return {
            "title": title,
            "content": content,
        }
    except Exception as e:
        log_message(f"Error processing commentary for {service.name}: {e}", "ERROR")
        return None


async def process_service(service: ZacksService, comment_id: int, proxy: str):
    """Process a single service's commentary"""
    try:
        proxy = f"http://{proxy}"
        raw_html = await fetch_commentary(service, comment_id, proxy)

        if raw_html is None:
            return False

        commentary = process_commentary(raw_html, service)

        if commentary:
            current_time = datetime.now(pytz.utc)
            message = (
                f"<b>New Zacks Commentary - {service.name}!</b>\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>Comment Id:</b> {comment_id}\n\n"
                f"<b>Title:</b> {commentary['title']}\n\n"
                f"{commentary['content'][:600]}\n\n\nthere is more......."
            )

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            return True

        return False
    except Exception as e:
        log_message(f"Error processing service {service.name}: {e}", "ERROR")
        return False


async def process_batch(proxies: list, comment_id: int):
    """Process all services concurrently using available proxies"""
    tasks = []
    for service in ZACKS_SERVICES:
        proxy = await get_available_proxy(proxies)
        tasks.append(process_service(service, comment_id, proxy))

    results = await asyncio.gather(*tasks)
    return any(results)  # Return True if any service found the commentary


async def run_scraper():
    """Main scraper loop that respects market hours"""
    global current_comment_id

    current_comment_id = load_last_comment_id()
    proxies = load_proxies()

    if not proxies:
        log_message("No proxies available", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting commentary monitoring...")

        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))
            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...")
                async with session_lock:
                    for session in proxy_sessions.values():
                        if not session.closed:
                            await session.close()
                    proxy_sessions.clear()
                break

            start_time = time()
            log_message(f"Checking comment ID: {current_comment_id}")

            try:
                found_commentary = await process_batch(proxies, current_comment_id)
                if found_commentary:
                    log_message(f"Found commentary for ID: {current_comment_id}")
                    current_comment_id += 1
                    await save_comment_id(current_comment_id)

                log_message(
                    f"Scan cycle completed in {time() - start_time:.2f} seconds"
                )
                await asyncio.sleep(CHECK_INTERVAL)

            except Exception as e:
                log_message(f"Error in scraper loop: {e}", "ERROR")
                async with session_lock:
                    for session in proxy_sessions.values():
                        if not session.closed:
                            await session.close()
                    proxy_sessions.clear()
                await asyncio.sleep(1)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ZACKS_USERNAME, ZACKS_PASSWORD]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        if current_comment_id:
            asyncio.run(save_comment_id(current_comment_id))
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
