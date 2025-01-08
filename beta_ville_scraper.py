import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

import pytz
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.gpt_ticker_extractor import TickerAnalysis, analyze_company_name_for_ticker
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open

load_dotenv()

# Constants
BETAVILLE_URL = "https://www.betaville.co.uk"
CHECK_INTERVAL = 1  # seconds
PROCESSED_POSTS_FILE = "data/betaville_processed_posts.json"
TELEGRAM_BOT_TOKEN = os.getenv("BETA_VILLE_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BETA_VILLE_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


class BetavillePost:
    def __init__(self, post_id: str, title: str, date: str, tags: List[str]):
        self.post_id = post_id
        self.title = title
        self.date = date
        self.tags = tags

    def to_dict(self) -> Dict:
        return {
            "post_id": self.post_id,
            "title": self.title,
            "date": self.date,
            "tags": self.tags,
        }


def load_processed_posts() -> Dict[str, Dict]:
    try:
        with open(PROCESSED_POSTS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_processed_posts(posts: Dict[str, Dict]):
    with open(PROCESSED_POSTS_FILE, "w") as f:
        json.dump(posts, f, indent=2)
    log_message("Processed posts saved.", "INFO")


def fetch_betaville_posts() -> List[BetavillePost]:
    try:
        response = requests.get(BETAVILLE_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        posts = []
        # Find all posts with both classes
        post_divs = soup.find_all(
            "div",
            class_=lambda x: x and "Post_post__v5D6j" in x and "Post_intel__l1FPV" in x,
        )

        for div in post_divs:
            # Extract post ID from URL
            link = div.find("a", class_="Post_intel__l1FPV")
            if not link:
                continue

            post_url = link.get("href", "")
            post_id = post_url.split("/")[-1] if post_url else None

            if not post_id:
                continue

            # Extract title
            title = link.text.strip()

            # Extract date
            date_span = div.find("span", class_="Post_date__panpL")
            date = date_span.text.strip() if date_span else ""

            # Extract tags
            tags = []
            tag_spans = div.find_all("span", class_="Post_tag__i0aZV")
            for tag_span in tag_spans:
                tag_link = tag_span.find("a")
                if tag_link:
                    tags.append(tag_link.text.strip())

            posts.append(BetavillePost(post_id, title, date, tags))

        log_message(f"Fetched {len(posts)} posts from Betaville", "INFO")
        return posts
    except Exception as e:
        log_message(f"Error fetching Betaville posts: {e}", "ERROR")
        return []


async def send_to_telegram(
    post: BetavillePost, ticker_obj: Optional[TickerAnalysis] = None
):

    message = f"<b>New Betaville Alert!</b>\n\n"
    message += f"<b>Post Time:</b> {post.date}\n"
    message += f"<b>Title:</b> {post.title}\n"
    message += f"<b>URL:</b> {BETAVILLE_URL}/betaville-intelligence/{post.post_id}\n"

    if post.tags:
        message += f"<b>Tags:</b> {', '.join(post.tags)}\n"

    if ticker_obj and ticker_obj.found:
        message += f"\n<b>Ticker:</b> {ticker_obj.ticker}\n"
        message += f"<b>Company:</b> {ticker_obj.company_name}\n"
        message += f"<b>Confidence:</b> {ticker_obj.confidence}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Post sent to Telegram: {post.post_id}", "INFO")


async def run_scraper():
    processed_posts = load_processed_posts()

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
            posts = fetch_betaville_posts()

            new_posts = [post for post in posts if post.post_id not in processed_posts]

            if new_posts:
                log_message(f"Found {len(new_posts)} new posts to process.", "INFO")

                for post in new_posts:
                    # Analyze title for ticker
                    ticker_obj = await analyze_company_name_for_ticker(
                        post.tags, post.title
                    )

                    await send_to_telegram(post, ticker_obj)
                    processed_posts[post.post_id] = post.to_dict()

                save_processed_posts(processed_posts)
            else:
                log_message("No new posts found.", "INFO")

            await asyncio.sleep(CHECK_INTERVAL)


def main():
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
