import asyncio
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Set

import pytz
import requests
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
RSS_FEED_URL = "https://jehoshaphatresearch.com/category/research/feed"
CHECK_INTERVAL = 15
PROCESSED_GUIDS_FILE = "data/jehoshaphat_processed_guids.json"
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
            bypass = bypasser(RSS_FEED_URL, SESSION_FILE)

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


def load_processed_guids() -> Set[str]:
    try:
        with open(PROCESSED_GUIDS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_guids(guids: Set[str]) -> None:
    with open(PROCESSED_GUIDS_FILE, "w") as f:
        json.dump(list(guids), f, indent=2)
    log_message("Processed GUIDs saved.", "INFO")


def convert_to_chicago_time(time_str):
    try:
        dt = parsedate_to_datetime(time_str)
        chicago_tz = pytz.timezone("America/Chicago")
        chicago_time = dt.astimezone(chicago_tz)
        return chicago_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception as e:
        log_message(f"Error converting time: {e}", "ERROR")
        return time_str


def extract_ticker_from_title(title: str) -> Optional[str]:
    pattern = r"\(([A-Z]+(?:\.?[A-Z]+)*)\)"
    match = re.search(pattern, title)
    if match:
        return match.group(1)
    return None


def parse_rss_feed(rss_content: str) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(rss_content)
        items = root.findall(".//item")
        log_message(f"Found {len(items)} items in RSS feed", "INFO")
        posts_data = []
        for item in items:
            title = (
                item.find("title").text
                if item.find("title") is not None
                else "No Title"
            )
            link = item.find("link").text if item.find("link") is not None else None
            pub_date = (
                item.find("pubDate").text if item.find("pubDate") is not None else None
            )
            creator = (
                item.find(".//{http://purl.org/dc/elements/1.1/}creator").text
                if item.find(".//{http://purl.org/dc/elements/1.1/}creator") is not None
                else "Unknown"
            )
            ticker = extract_ticker_from_title(title)

            guid = item.find("guid").text if item.find("guid") is not None else None
            if link and pub_date:
                posts_data.append(
                    {
                        "title": title,
                        "link": link,
                        "pub_date": pub_date,
                        "chicago_time": convert_to_chicago_time(pub_date),
                        "creator": creator,
                        "ticker": ticker,
                        "guid": guid,
                    }
                )
        return posts_data
    except Exception as e:
        log_message(f"Error parsing RSS feed: {e}", "ERROR")
        return []


def fetch_rss_feed(cookies: Dict[str, Any]) -> tuple[Optional[str], Optional[Dict]]:
    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "Cache-Control": "max-age=0",
        }
        custom_cookies = {"cf_clearance": cookies["cf_clearance"]}

        response = requests.get(RSS_FEED_URL, headers=headers, cookies=custom_cookies)

        if response.status_code == 200:
            content = response.text
            log_message("Successfully fetched RSS feed", "INFO")
            return content, None
        elif response.status_code == 403:
            log_message(
                "Cloudflare clearance expired, refreshing cookies...", "WARNING"
            )
            cookies = load_cookies(frash=True)
            if not cookies:
                raise Exception("CF_CLEARANCE Failed: Feed")
            return None, cookies
        elif 500 <= response.status_code < 600:
            log_message(
                f"Server error {response.status_code}: Temporary issue, safe to ignore if infrequent.",
                "WARNING",
            )
            return None, None
        else:
            log_message(
                f"Failed to fetch RSS feed: HTTP {response.status_code}", "ERROR"
            )
            return None, None
    except Exception as e:
        if "CF_CLEARANCE Failed" in str(e):
            raise
        log_message(f"Error fetching RSS feed: {e}", "ERROR")
        return None, None


async def send_post_to_telegram(post: Dict[str, Any]) -> None:
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    title = post.get("title", "Unknown Title")
    link = post.get("link", "No Link")
    pub_date = post.get("chicago_time", post.get("pub_date", "Unknown Date"))
    creator = post.get("creator", "Unknown Author")
    ticker = post.get("ticker", "Unknown")

    message = f"<b>New Jehoshaphat Post - Research Feed</b>\n\n"
    message += f"<b>Time Found:</b> {timestamp}\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Publication Date:</b> {pub_date}\n"
    message += f"<b>Author:</b> {creator}\n"

    if ticker:
        message += f"<b>Ticker:</b> {ticker}\n"

    message += f"<b>URL:</b> {link}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"RSS post sent to Telegram: {title}", "INFO")


async def run_scraper() -> None:
    processed_guids = load_processed_guids()
    cookies = load_cookies()

    if not cookies:
        log_message("Failed to get valid cf_clearance", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()

        log_message(
            "Market is open. Starting to check RSS feed for new posts...", "DEBUG"
        )
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()

            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                break

            log_message("Checking RSS feed for new posts...")
            rss_content, pos_cookies = fetch_rss_feed(cookies)

            cookies = pos_cookies if pos_cookies is not None else cookies

            if rss_content:
                posts = parse_rss_feed(rss_content)
                new_posts = [
                    post
                    for post in posts
                    if post.get("guid") and post["guid"] not in processed_guids
                ]

                if new_posts:
                    log_message(f"Found {len(new_posts)} new posts in RSS feed", "INFO")
                    for post in new_posts:
                        await send_post_to_telegram(post)
                        if post.get("guid"):
                            processed_guids.add(post["guid"])

                    save_processed_guids(processed_guids)
                else:
                    log_message("No new posts found in RSS feed", "INFO")

            time.sleep(CHECK_INTERVAL)


def main() -> None:
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
