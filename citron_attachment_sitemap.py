import asyncio
import json
import os
import sys
from datetime import datetime

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
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

# Constants
CITRON_SITEMAP_URL = "https://citronresearch.com/attachment-sitemap.xml"
CHECK_INTERVAL = 5  # seconds
PROCESSED_ITEMS_FILE = "data/citron_processed_items.json"
TELEGRAM_BOT_TOKEN = os.getenv("CITRON_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("CITRON_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_processed_items():
    try:
        with open(PROCESSED_ITEMS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_processed_items(items_dict):
    with open(PROCESSED_ITEMS_FILE, "w") as f:
        json.dump(items_dict, f, indent=2)
    log_message("Processed items saved.", "INFO")


async def fetch_sitemap(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(CITRON_SITEMAP_URL, headers=headers) as response:
            if response.status == 200:
                sitemap_content = await response.text()
                log_message("Successfully fetched Citron sitemap", "INFO")
                return sitemap_content
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return None
            else:
                log_message(f"Failed to fetch sitemap: HTTP {response.status}", "ERROR")
                return None
    except Exception as e:
        log_message(f"Error fetching sitemap: {e}", "ERROR")
        return None


def convert_to_chicago_time(time_str):
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S%z")

        chicago_tz = pytz.timezone("America/Chicago")
        chicago_time = dt.astimezone(chicago_tz)

        return chicago_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception as e:
        log_message(f"Error converting time: {e}", "ERROR")
        return time_str


def parse_title_from_slug(url):
    try:
        if "/frontpage/" in url:
            slug = url.split("/frontpage/")[1].strip("/")
        else:
            slug = url.split("/")[-2]

        title = slug.replace("-", " ").title()
        return title
    except Exception as e:
        log_message(f"Error parsing title from slug: {e}", "ERROR")
        return url


def parse_sitemap(sitemap_content):
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

            # Only process frontpage items
            if "/frontpage/" in link:
                title = parse_title_from_slug(link)
                items_data.append(
                    {
                        "link": link,
                        "title": title,
                        "last_modified": last_modified,
                        "chicago_time": convert_to_chicago_time(last_modified),
                    }
                )

        return items_data
    except Exception as e:
        log_message(f"Error parsing sitemap: {e}", "ERROR")
        return []


def get_current_year():
    return datetime.now().year


async def send_item_to_telegram(item_data):
    chicago_time = item_data["chicago_time"]
    gmt_time = item_data["last_modified"]
    link = item_data["link"]
    title = item_data["title"]

    message = f"<b>New Citron Research Report</b>\n\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Link:</b> {link}\n"
    message += f"<b>Last Modified (GMT):</b> {gmt_time}\n"
    message += f"<b>Last Modified (Chicago):</b> {chicago_time}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Citron sitemap update sent to Telegram: {title} - {link}", "INFO")


async def run_citron_scraper():
    processed_items = load_processed_items()
    current_year = get_current_year()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            # await initialize_websocket()

            log_message(
                "Market is open. Starting to check for new Citron reports...", "DEBUG"
            )
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking for Citron sitemap updates...")
                sitemap_content = await fetch_sitemap(session)

                if sitemap_content:
                    items_data = parse_sitemap(sitemap_content)
                    updates_found = 0

                    for item_data in items_data:
                        link = item_data["link"]
                        last_modified = item_data["last_modified"]

                        # Check if this is from the current year
                        item_year = int(last_modified.split("-")[0])
                        if item_year != current_year:
                            continue

                        if (
                            link not in processed_items
                            or processed_items[link] != last_modified
                        ):
                            log_message(
                                f"New/Updated Citron report found: {item_data['title']}",
                                "INFO",
                            )
                            await send_item_to_telegram(item_data)
                            processed_items[link] = last_modified
                            updates_found += 1

                    if updates_found > 0:
                        log_message(
                            f"Found {updates_found} new or updated Citron reports",
                            "INFO",
                        )
                        save_processed_items(processed_items)
                    else:
                        log_message("No new Citron report updates found", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_citron_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
