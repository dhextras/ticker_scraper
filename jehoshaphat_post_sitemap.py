import asyncio
import json
import os
import re
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Set

import aiohttp
import pytz
from bs4 import BeautifulSoup
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
SITEMAP_URL = "https://jehoshaphatresearch.com/post-sitemap.xml"
CHECK_INTERVAL = 10
PROCESSED_URLS_FILE = "data/jehoshaphat_sitemap_processed_urls.json"
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
            bypass = bypasser(SITEMAP_URL, SESSION_FILE)

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


def load_processed_urls() -> Set[str]:
    try:
        with open(PROCESSED_URLS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_urls(urls: Set[str]) -> None:
    with open(PROCESSED_URLS_FILE, "w") as f:
        json.dump(list(urls), f, indent=2)
    log_message("Processed URLs saved.", "INFO")


def convert_to_chicago_time(time_str: str) -> str:
    try:
        if "T" in time_str:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        else:
            dt = parsedate_to_datetime(time_str)

        chicago_tz = pytz.timezone("America/Chicago")
        chicago_time = dt.astimezone(chicago_tz)
        return chicago_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception as e:
        log_message(f"Error converting time: {e}", "ERROR")
        return time_str


def extract_title_from_url(url: str) -> str:
    """Extract a readable title from the URL slug"""
    try:
        slug = url.rstrip("/").split("/")[-1]

        title = slug.replace("-", " ").title()

        title = re.sub(r"Is Short", "is Short", title)  # Don't capitalize "is"
        title = re.sub(r"On", "on", title)  # Don't capitalize "on"

        return title
    except Exception as e:
        log_message(f"Error extracting title from URL: {e}", "ERROR")
        return "Unknown Title"


def parse_sitemap(sitemap_content: str) -> List[Dict[str, Any]]:
    try:
        soup = BeautifulSoup(sitemap_content, "xml")
        urls = soup.find_all("url")
        log_message(f"Found {len(urls)} URLs in sitemap", "INFO")
        items_data = []

        for url in urls:
            link_element = url.find("loc")
            if not link_element:
                continue

            link = link_element.text
            lastmod_element = url.find("lastmod")
            if not lastmod_element:
                continue

            last_modified = lastmod_element.text

            image_element = url.find("image:loc")
            image_url = image_element.text if image_element else None

            title = extract_title_from_url(link)

            ticker_match = re.search(r"-([a-z]+)/?$", link.lower())
            ticker = ticker_match.group(1).upper() if ticker_match else None

            items_data.append(
                {
                    "link": link,
                    "title": title,
                    "last_modified": last_modified,
                    "chicago_time": convert_to_chicago_time(last_modified),
                    "image_url": image_url,
                    "possible_ticker": ticker,
                }
            )

        return items_data
    except Exception as e:
        log_message(f"Error parsing sitemap: {e}", "ERROR")
        return []


async def fetch_sitemap(
    session, cookies: Dict[str, Any]
) -> tuple[Optional[str], Optional[Dict]]:
    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "Cache-Control": "max-age=0",
            "Cookie": f"cf_clearance:{cookies['cf_clearance']}",
        }

        async with session.get(
            SITEMAP_URL, headers=headers, cookies=cookies
        ) as response:
            if response.status == 200:
                content = await response.text()
                log_message("Successfully fetched sitemap", "INFO")
                return content, None
            elif response.status == 403:
                log_message(
                    "Cloudflare clearance expired, refreshing cookies...", "WARNING"
                )
                cookies = load_cookies(frash=True)
                if not cookies:
                    raise Exception("CF_CLEARANCE Failed: Sitemap")
                return None, cookies
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return None, None
            else:
                log_message(f"Failed to fetch sitemap: HTTP {response.status}", "ERROR")
                return None, None
    except Exception as e:
        if "CF_CLEARANCE Failed" in str(e):
            raise
        log_message(f"Error fetching sitemap: {e}", "ERROR")
        return None, None


async def send_post_to_telegram(post: Dict[str, Any]) -> None:
    current_time = get_current_time()
    current_chicago_time = current_time.astimezone(
        pytz.timezone("America/Chicago")
    ).strftime("%Y-%m-%d %H:%M:%S %Z")

    title = post.get("title", "Unknown Title")
    link = post.get("link", "No Link")
    last_modified = post.get("chicago_time", "Unknown Date")
    image_url = post.get("image_url", None)
    possible_ticker = post.get("possible_ticker", None)

    message = f"<b>New Jehoshaphat Research Post Found (Sitemap)</b>\n\n"
    message += f"<b>Current Time:</b> {current_chicago_time}\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Last Modified:</b> {last_modified}\n"

    if possible_ticker:
        message += f"<b>Possible Ticker:</b> {possible_ticker}\n"

    message += f"<b>URL:</b> {link}\n"

    if image_url:
        message += f"<b>Image:</b> {image_url}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Sitemap post sent to Telegram: {title}", "INFO")


async def run_scraper() -> None:
    processed_urls = load_processed_urls()
    cookies = load_cookies()

    if not cookies:
        log_message("Failed to get valid cf_clearance", "CRITICAL")
        return

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()

            log_message(
                "Market is open. Starting to check sitemap for new posts...", "DEBUG"
            )
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking sitemap for new posts...")
                sitemap_content, pos_cookies = await fetch_sitemap(session, cookies)

                cookies = pos_cookies if pos_cookies is not None else cookies

                if sitemap_content:
                    posts = parse_sitemap(sitemap_content)
                    new_posts = [
                        post
                        for post in posts
                        if post.get("link") and post["link"] not in processed_urls
                    ]

                    if new_posts:
                        log_message(
                            f"Found {len(new_posts)} new posts in sitemap", "INFO"
                        )
                        for post in new_posts:
                            await send_post_to_telegram(post)
                            if post.get("link"):
                                processed_urls.add(post["link"])

                        save_processed_urls(processed_urls)
                    else:
                        log_message("No new posts found in sitemap", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


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
