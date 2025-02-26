import asyncio
import json
import os
import re
import sys
from pathlib import Path
from time import time
from typing import Optional

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
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("ZACKS_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("ZACKS_TELEGRAM_GRP")
ZACKS_USERNAME = os.getenv("ZACKS_USERNAME")
ZACKS_PASSWORD = os.getenv("ZACKS_PASSWORD")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
CHECK_INTERVAL = 0.2  # seconds
STARTING_CID = 43250  # Starting comment ID

DATA_DIR = Path("data")
COMMENT_ID_FILE = DATA_DIR / "zacks_last_comment_id.json"

# Session management
session: Optional[aiohttp.ClientSession] = None
session_lock = asyncio.Lock()


def extract_ticker(title, content):
    if title == "We're Buying and Selling Today":
        buy_section = re.search(r"(Buy .*? Today)", content)
        if buy_section:
            match = re.search(r"\(([A-Z]+)\)", content[buy_section.start() :])
            if match:
                return match.group(1), "Buy"
    elif "BUY" in title or "Buy" in title or "Buying" in title:
        if "sell" in title.lower():
            match = re.search("buy", content.lower())
            match2 = re.search("hold", content.lower())
            if match:
                content = content[match.end() :]
            elif match2:
                content = content[match2.end() :]
        match = re.search(r"\(([A-Z]+)\)", content)
        if match:
            return match.group(1), "Buy"
    elif "Adding" in title:
        match = re.search(r"Adding\s+([A-Z]+)", title)
        if match:
            return match.group(1), "Buy"
    # TODO: Later also process sell alerts

    return None, None


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
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(COMMENT_ID_FILE, "w") as f:
            json.dump({"last_comment_id": comment_id}, f)
    except Exception as e:
        log_message(f"Error saving comment ID: {e}", "ERROR")


async def create_session():
    """Create and return a new session with login"""
    try:
        new_session = aiohttp.ClientSession()

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

        async with new_session.post(
            "https://www.zacks.com/tradingservices/index.php",
            headers=headers,
            data=data,
        ) as response:
            if response.status != 200:
                await new_session.close()
                return None

        return new_session
    except Exception as e:
        log_message(f"Error creating session: {e}", "ERROR")
        return None


async def get_or_create_session():
    """Get existing session or create new one"""
    global session
    async with session_lock:
        if session and not session.closed:
            return session

        new_session = await create_session()
        if new_session:
            session = new_session
        return session


async def fetch_commentary(comment_id: int):
    """Fetch commentary for Zacks Confidential"""
    global session

    current_session = await get_or_create_session()
    if not current_session:
        return None

    url = f"https://www.zacks.com/confidential/commentary.php?cid={comment_id}"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0",
    }

    try:
        async with current_session.get(url, headers=headers) as response:
            if response.status != 200:
                if response.status in [401, 403]:
                    # Session expired, create new session
                    async with session_lock:
                        if session and not session.closed:
                            await session.close()
                            session = None
                return None
            return await response.text()
    except Exception as e:
        log_message(f"Error fetching commentary: {e}", "ERROR")
        return None


def process_commentary(html: str):
    """Extract title and content from commentary HTML"""
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Find the title and content using the new selectors
        title_elem = soup.select_one("#cdate-most-recent > article > div > h2")
        content_elem = soup.select_one("#cdate-most-recent > article > div")

        if not title_elem or not content_elem:
            return None

        title = title_elem.get_text(strip=True)
        content = content_elem.get_text(strip=True)

        if title in content:
            content = content.replace(title, "", 1)

        if not title or not content:
            return None

        ticker, action = extract_ticker(title, content)

        return {"title": title, "content": content, "ticker": ticker, "action": action}
    except Exception as e:
        log_message(f"Error processing commentary: {e}", "ERROR")
        return None


async def run_scraper():
    """Main scraper loop that respects market hours"""
    global session
    current_comment_id = load_last_comment_id()

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting commentary monitoring...", "DEBUG")

        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()
            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                async with session_lock:
                    if session and not session.closed:
                        await session.close()
                        session = None
                break

            start_time = time()
            log_message(f"Checking comment ID: {current_comment_id}")

            try:
                raw_html = await fetch_commentary(current_comment_id)
                if raw_html:
                    commentary = process_commentary(raw_html)
                    if commentary:
                        current_time = get_current_time()

                        ticker_info = ""
                        if commentary["ticker"] and commentary["action"]:
                            ticker_info = f"\n<b>Action:</b> {commentary['action']} {commentary['ticker']}"

                            await send_ws_message(
                                {
                                    "name": "Zacks - Commentary",
                                    "type": commentary["action"],
                                    "ticker": commentary["ticker"],
                                    "sender": "zacks",
                                    "target": "CSS",
                                },
                                WS_SERVER_URL,
                            )

                        message = (
                            f"<b>New Zacks Commentary!</b>\n"
                            f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                            f"<b>Comment Id:</b> {current_comment_id}{ticker_info}\n\n"
                            f"<b>Title:</b> {commentary['title']}\n\n"
                            f"{commentary['content'][:600]}\n\n\nthere is more......."
                        )

                        await send_telegram_message(
                            message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                        )
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
                    if session and not session.closed:
                        await session.close()
                        session = None
                await asyncio.sleep(1)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ZACKS_USERNAME, ZACKS_PASSWORD]):
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
