import asyncio
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

XML_FEED_URL = "https://thebearcave.substack.com/feed"
CHECK_INTERVAL = 0.05
PROCESSED_URLS_FILE = "data/bearcave_xml_processed_urls.json"
PROXY_FILE = "cred/proxies.json"
TELEGRAM_BOT_TOKEN = os.getenv("BEARCAVE_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BEARCAVE_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

os.makedirs("data", exist_ok=True)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.59",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 OPR/78.0.4093.112",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/91.0.4472.80 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
]


def load_proxies():
    try:
        with open(PROXY_FILE, "r") as f:
            data = json.load(f)
            return data["bearcave_xml"]
    except Exception as e:
        log_message(f"Error loading proxies: {e}", "ERROR")
        return []


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


def get_random_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": str(uuid.uuid4()),
        "X-Request-Time": str(int(time.time())),
    }


def get_random_cache_buster():
    cache_busters = [
        ("timestamp", lambda: int(time.time() * 10000)),
        ("request_uuid", lambda: str(uuid.uuid4())),
        ("cache_time", lambda: int(time.time())),
        ("unique", lambda: f"{int(time.time())}-{random.randint(1000, 9999)}"),
    ]
    variable, value_generator = random.choice(cache_busters)
    return f"{variable}={value_generator()}"


async def fetch_xml_feed(session, raw_proxy=None):
    headers = get_random_headers()
    random_cache_buster = get_random_cache_buster()
    proxy = raw_proxy if raw_proxy is None else f"http://{raw_proxy}"

    try:
        async with session.get(
            f"{XML_FEED_URL}?{random_cache_buster}",
            headers=headers,
            proxy=proxy,
            timeout=1,
        ) as response:
            if response.status == 200:
                content = await response.text()
                soup = BeautifulSoup(content, "xml")
                posts = []
                for item in soup.find_all("item"):
                    title = item.find("title").text.strip()
                    title = re.sub(
                        r"^\s*\[\[CDATA\[(.*?)\]\]\s*$", r"\1", title, flags=re.DOTALL
                    )
                    pub_date_str = item.find("pubDate").text.strip()
                    pub_date = datetime.strptime(
                        pub_date_str, "%a, %d %b %Y %H:%M:%S %Z"
                    )
                    pub_date_iso = pub_date.isoformat() + "Z"
                    posts.append(
                        {
                            "title": title,
                            "canonical_url": item.find("link").text.strip(),
                            "post_date": pub_date_iso,
                        }
                    )
                log_message(
                    f"Fetched {len(posts)} posts from XML using proxy: {raw_proxy}",
                    "INFO",
                )
                return posts
            else:
                log_message(f"Failed to fetch XML: HTTP {response.status}", "ERROR")
                return []
    except Exception as e:
        log_message(f"Error fetching XML with proxy {raw_proxy}: {e}", "ERROR")
        return []


def extract_ticker(title):
    if title is not None and title.startswith("Problems at"):
        match = re.search(r"\((.*?)\)", title)
        if match:
            potential_ticker = match.group(1)
            if potential_ticker.isupper():
                return potential_ticker
    return None


async def send_to_telegram(post_data, ticker=None):
    current_time = datetime.now(pytz.timezone("US/Eastern"))
    post_date = datetime.fromisoformat(post_data["post_date"].replace("Z", "+00:00"))
    post_date_est = post_date.astimezone(pytz.timezone("US/Eastern"))

    message = f"<b>New Bear Cave Article - XML!</b>\n\n"
    message += (
        f"<b>Published Date:</b> {post_date_est.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    )
    message += f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Title:</b> {post_data['title']}\n"
    message += f"<b>URL:</b> {post_data['canonical_url']}\n"

    if ticker:
        message += f"<b>Ticker:</b> {ticker}\n"
        await send_ws_message(
            {
                "name": "The Bear Cave - X",
                "type": "Sell",
                "ticker": ticker,
                "sender": "bearcave",
            },
            WS_SERVER_URL,
        )
        log_message(
            f"Ticker sent to WebSocket: {ticker} - {post_data['canonical_url']}", "INFO"
        )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Article sent to Telegram: {post_data['canonical_url']}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()
    proxies = load_proxies()

    if not proxies:
        log_message("No proxies available. Running without proxies.", "WARNING")
        proxies = [None]

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
                proxy = random.choice(proxies)
                posts = await fetch_xml_feed(session, proxy)

                new_posts = [
                    post
                    for post in posts
                    if post.get("canonical_url")
                    and post["canonical_url"] not in processed_urls
                ]

                if new_posts:
                    log_message(f"Found {len(new_posts)} new posts to process.", "INFO")
                    for post in new_posts:
                        if not "title" in post:
                            post["title"] = "No title found"
                        ticker = extract_ticker(post["title"])
                        await send_to_telegram(post, ticker)
                        processed_urls.add(post["canonical_url"])
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
