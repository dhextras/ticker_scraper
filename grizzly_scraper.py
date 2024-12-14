import asyncio
import io
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp
import pytz
from dotenv import load_dotenv
from pdfminer.high_level import extract_text
from utils.bypass_cloudflare import bypasser
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
JSON_URL = "https://grizzlyreports.com/wp-json/wp/v2/media"
CHECK_INTERVAL = 3  # seconds
PROCESSED_URLS_FILE = "data/grizzly_processed_urls.json"
SESSION_FILE = "data/grizzly_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("GRIZZLY_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("GRIZZLY_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

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
            bypass = bypasser(JSON_URL, SESSION_FILE)

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
    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "Cache-Control": "max-age=0",
            "Cookie": f"cf_clearance:{cookies['cf_clearance']}",
        }

        async with session.get(JSON_URL, headers=headers, cookies=cookies) as response:
            if response.status == 200:
                data = await response.json()
                log_message(f"Fetched {len(data)} posts from JSON", "INFO")
                return data, None
            elif response.status == 403:
                log_message(
                    "CF_CLEARANCE expired trying to revalidate it while fetching json",
                    "ERROR",
                )
                cookies = load_cookies(frash=True)
                if not cookies:
                    raise Exception("CF_CLEARANCE Failed: Post")
                return [], cookies
            else:
                log_message(f"Failed to fetch JSON: HTTP {response.status}", "ERROR")
                return [], None
    except Exception as e:
        if "CF_CLEARANCE Failed" in str(e):
            raise
        log_message(f"Error fetching JSON: {e}", "ERROR")
        return [], None


async def extract_ticker_from_pdf(session, url, cookies):
    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "Cache-Control": "max-age=0",
            "Cookie": f"cf_clearance:{cookies['cf_clearance']}",
        }

        async with session.get(url, headers=headers, cookies=cookies) as response:
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
                            return match, None

                log_message(f"No ticker found in PDF: {url}", "WARNING")
                return None, None
            elif response.status == 403:
                log_message(
                    f"CF_CLEARANCE expired trying to revalidate it while fetching url: {url}",
                    "ERROR",
                )
                cookies = load_cookies(frash=True)
                if not cookies:
                    raise Exception(f"CF_CLEARANCE Failed, url: {url}")
                return None, cookies
            else:
                log_message(f"Failed to fetch PDF: HTTP {response.status}", "ERROR")
                return None, None
    except Exception as e:
        log_message(f"Error processing PDF {url}: {e}", "ERROR")
        return None, None


async def send_posts_to_telegram(urls):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S")
    joined_urls = "\n  ".join(urls)

    message = f"<b>New Grizzly Reports medias found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Media URLS:</b>\n  {joined_urls}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New Posts sent to Telegram: {urls}", "INFO")


async def send_to_telegram(url, ticker):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Grizzly Reports Ticker found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Ticker:</b> {ticker}\n"

    await send_ws_message(
        {
            "name": "Grizzly Reports",
            "type": "Sell",
            "ticker": ticker,
            "sender": "grizzly",
        },
        WS_SERVER_URL,
    )
    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Report sent to Telegram and WebSocket: {ticker} - {url}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()
    cookies = load_cookies()

    if not cookies:
        log_message("Failed to get valid cf_clearance", "CRITICAL")
        return

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
                posts, pos_cookies = await fetch_json(session, cookies)

                cookies = pos_cookies if pos_cookies is not None else cookies
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

                    for url in new_urls:
                        if url.lower().endswith(".pdf"):
                            ticker, pos_cookies = await extract_ticker_from_pdf(
                                session, url, cookies
                            )

                            cookies = pos_cookies if pos_cookies is not None else cookies
                            if ticker:
                                await send_to_telegram(url, ticker)
                        processed_urls.add(url)

                    await send_posts_to_telegram(new_urls)
                    save_processed_urls(processed_urls)
                else:
                    log_message("No new posts found.", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all(
        [TELEGRAM_BOT_TOKEN, TELEGRAM_GRP, WS_SERVER_URL]
    ):
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
