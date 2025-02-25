import asyncio
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime

import pytz
import requests
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()

# Constants
API_URL = "https://substack.com/api/v1/community/publications/836125/posts"
CHECK_INTERVAL = 5  # seconds
PROCESSED_IDS_FILE = "data/substack_citrini_processed_ids.json"
COOKIE_FILE = "cred/substack_cookies.json"
TELEGRAM_BOT_TOKEN = os.getenv("BEARCAVE_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BEARCAVE_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

os.makedirs("data", exist_ok=True)

# User agents list
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


def load_cookies():
    """Load substack.sid cookie from json file"""
    try:
        with open(COOKIE_FILE, "r") as f:
            data = json.load(f)
            return data.get("sid")
    except Exception as e:
        log_message(f"Error loading cookies: {e}", "ERROR")
        return None


def load_processed_ids():
    try:
        with open(PROCESSED_IDS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_ids(ids):
    with open(PROCESSED_IDS_FILE, "w") as f:
        json.dump(list(ids), f, indent=2)
    log_message("Processed IDs saved.", "INFO")


def get_random_headers():
    """Generate random headers for requests"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": str(uuid.uuid4()),
        "X-Request-Time": str(int(time.time())),
    }


async def fetch_posts(sid=None):
    """Fetch posts from Substack community API"""
    if not sid:
        log_message("No Substack SID cookie available", "ERROR")
        return []

    headers = get_random_headers()
    cookies = {"substack.sid": sid}

    try:
        response = requests.get(
            f"{API_URL}",
            headers=headers,
            cookies=cookies,
        )
        if response.status_code == 200:
            data = response.json()
            log_message(f"Fetched posts from Substack API", "INFO")

            # Check if there are threads in the response
            if "threads" not in data:
                log_message("No threads found in response", "WARNING")
                return []

            return data["threads"]
        else:
            log_message(f"Failed to fetch posts: HTTP {response.status_code}", "ERROR")
            return []
    except Exception as e:
        log_message(f"Error fetching posts: {e}", "ERROR")
        return []


def is_paywalled(post_data):
    """Check if the post is paywalled"""
    community_post = post_data.get("communityPost", {})

    paywall_info = community_post.get("paywallInfo")
    body = community_post.get("body")

    return paywall_info is not None or body is None


def extract_media_urls(post_data):
    """Extract media URLs from the post"""
    community_post = post_data.get("communityPost", {})
    media_assets = community_post.get("media_assets", [])

    return [asset.get("url") for asset in media_assets if asset.get("url")]


async def send_to_telegram(post_data):
    """Send post information to Telegram"""
    community_post = post_data.get("communityPost", {})

    current_time = get_current_time()
    created_at = datetime.fromisoformat(
        community_post.get("created_at", "").replace("Z", "+00:00")
    )
    created_at_est = created_at.astimezone(pytz.timezone("US/Eastern"))
    updated_at = datetime.fromisoformat(
        community_post.get("updated_at", "").replace("Z", "+00:00")
    )
    updated_at_est = updated_at.astimezone(pytz.timezone("US/Eastern"))

    post_id = community_post.get("id", "Unknown")
    body = community_post.get("body", "No content available")

    user = post_data.get("user", {})
    author_name = user.get("name", "Unknown")
    media_urls = extract_media_urls(post_data)

    message = f"<b>New Substack Post from {author_name}</b>\n\n"
    message += f"<b>ID:</b> {post_id}\n"
    message += f"<b>Created:</b> {created_at_est.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Updated:</b> {updated_at_est.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Current:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
    message += f"<b>Content:</b>\n{body}\n\n"

    if media_urls:
        message += "<b>Media:</b>\n"
        for url in media_urls:
            message += f"{url}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Post sent to Telegram: {post_id}", "INFO")


async def run_scraper():
    processed_ids = load_processed_ids()
    sid = load_cookies()

    if not sid:
        log_message("No Substack SID cookie available. Exiting.", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new posts...", "DEBUG")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()

            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                break

            log_message("Checking for new posts...")
            posts = await fetch_posts(sid)

            new_posts = []
            for post in posts:
                community_post = post.get("communityPost", {})
                post_id = community_post.get("id")

                if not post_id or post_id in processed_ids:
                    continue

                if is_paywalled(post):
                    log_message(f"Post {post_id} is paywalled, skipping", "WARNING")
                    # Still mark as processed to avoid checking again
                    processed_ids.add(post_id)
                    continue

                new_posts.append(post)

            if new_posts:
                log_message(f"Found {len(new_posts)} new posts to process.", "INFO")

                for post in new_posts:
                    community_post = post.get("communityPost", {})
                    post_id = community_post.get("id")

                    await send_to_telegram(post)
                    processed_ids.add(post_id)

                save_processed_ids(processed_ids)
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
