import asyncio
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp
import pytz
from dotenv import load_dotenv
from pdfminer.high_level import extract_text
from selenium.webdriver.chrome.options import Options
from seleniumrequests import Chrome

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
JSON_URL = "https://www.muddywatersresearch.com/wp-json/wp/v2/media"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/muddy_waters_processed_urls.json"
SESSION_FILE = "data/muddy_waters_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("MUDDY_WATERS_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("MUDDY_WATERS_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

os.makedirs("data", exist_ok=True)


def get_browser_session(driver):
    """Get browser session data (cookies and localStorage)"""
    try:
        cookies = driver.get_cookies()
        return {
            "cookies": cookies,
        }
    except Exception as e:
        log_message(f"Error getting browser session: {e}", "ERROR")
        return None


async def get_new_session():
    """Initialize browser and get new session cookies"""
    options = Options()
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--maximize-window")
    options.add_argument("--disable-search-engine-choice-screen")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    driver = Chrome(options=options)
    try:
        driver.get(JSON_URL)
        while True:
            cookies = driver.get_cookies()
            if any(
                cookie["name"] == "cf_clearance" and cookie["value"]
                for cookie in cookies
            ):
                break
            time.sleep(5)

        session_data = get_browser_session(driver)
        if session_data:
            save_session_credentials(session_data)
            return session_data

    except Exception as e:
        log_message(f"Error getting new session: {e}", "ERROR")
    finally:
        driver.quit()
    return None


def load_session_credentials() -> Optional[Dict[str, Any]]:
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r") as f:
                session = json.load(f)
                return session
    except Exception as e:
        log_message(f"Error loading session: {e}", "ERROR")
    return None


def save_session_credentials(session: Dict[str, Any]):
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(session, f)
    except Exception as e:
        log_message(f"Error saving session: {e}", "ERROR")


def load_processed_urls():
    try:
        with open(PROCESSED_URLS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_urls(urls):
    with open(PROCESSED_URLS_FILE, "w") as f:
        json.dump(list(urls), f)
    log_message("Processed URLs saved.", "INFO")


async def fetch_json(session, cookies):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        async with session.get(JSON_URL, headers=headers, cookies=cookies) as response:
            if response.status == 200:
                data = await response.json()
                log_message(f"Fetched {len(data)} posts from JSON", "INFO")
                return data
            else:
                log_message(f"Failed to fetch JSON: HTTP {response.status}", "ERROR")
                return []
    except Exception as e:
        log_message(f"Error fetching JSON: {e}", "ERROR")
        return []


async def extract_ticker_from_pdf(session, url, cookies):
    try:
        async with session.get(url, cookies=cookies) as response:
            if response.status == 200:
                pdf_content = await response.read()
                pdf_file = io.BytesIO(pdf_content)

                first_page = extract_text(pdf_file, page_numbers=[0])

                # Look for capitalized words after brackets
                bracket_pattern = r"\((.*?)\)"
                matches = re.findall(bracket_pattern, first_page)

                if matches:
                    # Take the first all-caps word after a bracket
                    for match in matches:
                        if match.isupper():
                            return match

                log_message(f"No ticker found in PDF: {url}", "WARNING")
                return None
            else:
                log_message(f"Failed to fetch PDF: HTTP {response.status}", "ERROR")
                return None
    except Exception as e:
        log_message(f"Error processing PDF {url}: {e}", "ERROR")
        return None


async def send_posts_to_telegram(urls):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S")
    joined_urls = "\n  ".join(urls)

    message = f"<b>New Muddy Waters medias found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Media URLS:</b>\n  {joined_urls}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New Posts sent to Telegram: {urls}", "INFO")


async def send_to_telegram(url, ticker):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Muddy Waters Ticker found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Ticker:</b> {ticker}\n"

    await send_ws_message(
        {
            "name": "Muddy Waters",
            "type": "Sell",
            "ticker": ticker,
            "sender": "muddy_waters",
        },
        WS_SERVER_URL,
    )
    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Report sent to Telegram and WebSocket: {ticker} - {url}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()
    session_data = load_session_credentials()

    if not session_data:
        session_data = await get_new_session()
        if not session_data:
            log_message("Failed to get initial session", "CRITICAL")
            return

    cookies = {cookie["name"]: cookie["value"] for cookie in session_data["cookies"]}

    async with aiohttp.ClientSession() as session:
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
                posts = await fetch_json(session, cookies)

                if not posts:
                    log_message("Failed to fetch posts or no posts returned", "ERROR")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                new_urls = [
                    post["source_url"]
                    for post in posts
                    if post.get("source_url")
                    and post["source_url"] not in processed_urls
                ]

                if new_urls:
                    log_message(f"Found {len(new_urls)} new posts to process.", "INFO")
                    await send_posts_to_telegram(new_urls)

                    for url in new_urls:
                        if url.lower().endswith(".pdf"):
                            ticker = await extract_ticker_from_pdf(
                                session, url, cookies
                            )
                            if ticker:
                                await send_to_telegram(url, ticker)
                        processed_urls.add(url)
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
