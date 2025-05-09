import asyncio
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import aiohttp
import pytz
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
RSS_FEED_URL = "http://kerrisdalecap.com/investments/feed"
CHECK_INTERVAL = 5  # seconds
PROCESSED_POSTS_FILE = "data/kerrisdale_processed_posts.json"
TELEGRAM_BOT_TOKEN = os.getenv("KERRISDALE_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("KERRISDALE_TELEGRAM_GRP")

# XML namespace mappings
NAMESPACES = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "wfw": "http://wellformedweb.org/CommentAPI/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
    "sy": "http://purl.org/rss/1.0/modules/syndication/",
    "slash": "http://purl.org/rss/1.0/modules/slash/",
}

os.makedirs("data", exist_ok=True)


def load_processed_posts():
    try:
        with open(PROCESSED_POSTS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_processed_posts(posts_dict):
    with open(PROCESSED_POSTS_FILE, "w") as f:
        json.dump(posts_dict, f, indent=2)
    log_message("Processed posts saved.", "INFO")


async def fetch_rss_feed(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(RSS_FEED_URL, headers=headers) as response:
            if response.status == 200:
                rss_content = await response.text()
                log_message("Successfully fetched RSS feed", "INFO")
                return rss_content
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return None
            else:
                log_message(
                    f"Failed to fetch RSS feed: HTTP {response.status}", "ERROR"
                )
                return None
    except Exception as e:
        log_message(f"Error fetching RSS feed: {e}", "ERROR")
        return None


def convert_to_chicago_time(time_str):
    try:
        dt = parsedate_to_datetime(time_str)

        chicago_tz = pytz.timezone("America/Chicago")
        chicago_time = dt.astimezone(chicago_tz)

        return chicago_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception as e:
        log_message(f"Error converting time: {e}", "ERROR")
        return time_str


def parse_rss_feed(rss_content):
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

            ticker = None
            category_elem = item.find("category")
            if category_elem is not None and category_elem.text:
                ticker = category_elem.text.strip()

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


async def send_post_to_telegram(post_data):
    title = post_data["title"]
    link = post_data["link"]
    pub_date = post_data["pub_date"]
    chicago_time = post_data["chicago_time"]
    ticker = post_data["ticker"]
    creator = post_data["creator"]

    message = f"<b>New Kerrisdale Investment Post</b>\n\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Link:</b> {link}\n"
    message += f"<b>Ticker:</b> {ticker}\n"
    message += f"<b>Author:</b> {creator}\n"
    message += f"<b>Published (GMT):</b> {pub_date}\n"
    message += f"<b>Published (Chicago):</b> {chicago_time}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New post sent to Telegram: {title} - {link}", "INFO")

    # TODO:
    """
    if ticker:
        await send_ws_message(
            {
                "name": "Kerrisdale Capital Investment",
                "type": "New Post",
                "ticker": ticker,
                "sender": "kerrisdale_rss",
                "target": "CSS",
            },
        )
    """


async def run_rss_scraper():
    processed_posts = load_processed_posts()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            # await initialize_websocket()

            log_message("Market is open. Starting to check for new posts...", "DEBUG")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking for new RSS posts...")
                rss_content = await fetch_rss_feed(session)

                if rss_content:
                    posts_data = parse_rss_feed(rss_content)
                    new_posts_found = 0

                    for post_data in posts_data:
                        guid = post_data["guid"]
                        pub_date = post_data["pub_date"]

                        if (
                            guid not in processed_posts
                            or processed_posts[guid] != pub_date
                        ):
                            log_message(
                                f"New/Updated post found: {post_data['title']}", "INFO"
                            )
                            await send_post_to_telegram(post_data)
                            processed_posts[guid] = pub_date
                            new_posts_found += 1

                    if new_posts_found > 0:
                        log_message(
                            f"Found {new_posts_found} new or updated posts", "INFO"
                        )
                        save_processed_posts(processed_posts)
                    else:
                        log_message("No new posts found", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_rss_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
