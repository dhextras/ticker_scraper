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
TAG_SITEMAP_URL = "https://www.kerrisdalecap.com/post_tag-sitemap.xml"
CHECK_INTERVAL = 5  # seconds
PROCESSED_TAGS_FILE = "data/kerrisdale_processed_tags.json"
TELEGRAM_BOT_TOKEN = os.getenv("KERRISDALE_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("KERRISDALE_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_processed_tags():
    try:
        with open(PROCESSED_TAGS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_processed_tags(tags_dict):
    with open(PROCESSED_TAGS_FILE, "w") as f:
        json.dump(tags_dict, f, indent=2)
    log_message("Processed tags saved.", "INFO")


async def fetch_sitemap(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(TAG_SITEMAP_URL, headers=headers) as response:
            if response.status == 200:
                sitemap_content = await response.text()
                log_message("Successfully fetched tag sitemap", "INFO")
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
        # Parse the GMT time string
        dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S%z")

        chicago_tz = pytz.timezone("America/Chicago")
        chicago_time = dt.astimezone(chicago_tz)

        return chicago_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception as e:
        log_message(f"Error converting time: {e}", "ERROR")
        return time_str


def parse_sitemap_tags(sitemap_content):
    try:
        soup = BeautifulSoup(sitemap_content, "xml")

        urls = soup.find_all("url")
        log_message(f"Found {len(urls)} URLs in sitemap", "INFO")

        tags_data = []
        for url in urls:
            link = url.find("loc").text
            last_modified = url.find("lastmod").text if url.find("lastmod") else None

            if "/tag/" in link:
                tag = link.split("/tag/")[1].strip("/")
                tags_data.append(
                    {
                        "link": link,
                        "tag": tag,
                        "last_modified": last_modified,
                        "chicago_time": convert_to_chicago_time(last_modified),
                    }
                )

        return tags_data
    except Exception as e:
        log_message(f"Error parsing sitemap: {e}", "ERROR")
        return []


def get_current_year():
    return datetime.now().year


async def send_tag_to_telegram(tag_data):
    chicago_time = tag_data["chicago_time"]
    gmt_time = tag_data["last_modified"]
    link = tag_data["link"]
    tag = tag_data["tag"]

    current_time = get_current_time()

    message = f"<b>New Kerrisdale Tag</b>\n\n"
    message += f"<b>Tag:</b> {tag}\n"
    message += f"<b>Link:</b> {link}\n"
    message += f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Last Modified (GMT):</b> {gmt_time}\n"
    message += f"<b>Last Modified (Chicago):</b> {chicago_time}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Tag update sent to Telegram: {tag} - {link}", "INFO")

    # TODO:
    """
    await send_ws_message(
        {
            "name": "Kerrisdale Capital Tag",
            "type": "Sell",
            "ticker": tag.upper() if tag.isupper() else tag,
            "sender": "kerrisdale_tags",
            "target": "CSS",
        },
    )
    """


def parse_ticker(tag):
    # TODO:
    pass


async def run_tag_scraper():
    processed_tags = load_processed_tags()
    current_year = get_current_year()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            # await initialize_websocket()

            log_message(
                "Market is open. Starting to check for new tag updates...", "DEBUG"
            )
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking for tag updates...")
                sitemap_content = await fetch_sitemap(session)

                if sitemap_content:
                    tags_data = parse_sitemap_tags(sitemap_content)
                    updates_found = 0

                    for tag_data in tags_data:
                        tag = tag_data["tag"]
                        last_modified = tag_data["last_modified"]

                        # Check if this is from the current year
                        tag_year = int(last_modified.split("-")[0])
                        if tag_year != current_year:
                            continue

                        # Check if this is a new or updated tag
                        if (
                            tag not in processed_tags
                            or processed_tags[tag] != last_modified
                        ):
                            log_message(f"New/Updated tag found: {tag}", "INFO")
                            await send_tag_to_telegram(tag_data)
                            processed_tags[tag] = last_modified
                            updates_found += 1

                    if updates_found > 0:
                        log_message(
                            f"Found {updates_found} new or updated tags", "INFO"
                        )
                        save_processed_tags(processed_tags)
                    else:
                        log_message("No new tag updates found", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_tag_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
