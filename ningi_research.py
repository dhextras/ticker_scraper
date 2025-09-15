import asyncio
import json
import os
import re
import sys
from time import time

from dotenv import load_dotenv
from DrissionPage import ChromiumOptions, ChromiumPage

from utils.bypass_cloudflare import bypasser
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
JSON_URL = "https://ningiresearch.com/wp-json/wp/v2/posts"
CHECK_INTERVAL = 2  # seconds
PROCESSED_URLS_FILE = "data/ningi_processed_urls.json"
SESSION_FILE = "data/ningi_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("NINGI_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("NINGI_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)

# Initialize browser once
co = ChromiumOptions()
page = ChromiumPage(co)


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


def extract_ticker_from_title(title):
    try:
        pattern = r"\(([A-Z]+):\s*([A-Z0-9]+(?:\.[A-Z])?)\)"
        matches = re.findall(pattern, title)

        if matches:
            exchange, ticker = matches[0]
            ticker = ticker.split(".")[0]
            log_message(f"Extracted ticker: {ticker} from exchange: {exchange}", "INFO")
            return ticker, exchange

        log_message(f"No ticker found in title: {title}", "WARNING")
        return None, None
    except Exception as e:
        log_message(f"Error extracting ticker from title '{title}': {e}", "ERROR")
        return None, None


async def fetch_json():
    global page, co

    try:
        start_time = time()
        page.get(JSON_URL)
        if "just a moment" in page.title.lower():
            bypass = bypasser(JSON_URL, SESSION_FILE)
            if not bypass or bypass is False:
                log_message(
                    "Failed to bypass cloudflare, ignore if infrequent", "WARNING"
                )
                return []

            try:
                page.quit()
            except:
                pass

            co = ChromiumOptions()
            page = ChromiumPage(co)
            page.get(JSON_URL)

        data = page.json
        log_message(
            f"Fetched {len(data)} posts from JSON in {time() - start_time:2f}", "INFO"
        )
        return data
    except Exception as e:
        log_message(f"Error fetching JSON: {e}", "ERROR")
        return []


async def send_posts_to_telegram(posts_data):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Ningi Research posts found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Posts:</b>\n"

    for post in posts_data:
        title = post["title"]
        link = post["link"]
        date_gmt = post["date_gmt"]
        message += f"  • <a href='{link}'>{title}</a>\n    Date: {date_gmt}\n\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New Posts sent to Telegram: {len(posts_data)} posts", "INFO")


async def send_to_telegram(post_data, ticker, exchange):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
    title = post_data["title"]
    link = post_data["link"]
    date_gmt = post_data["date_gmt"]

    message = f"<b>New Ningi Research Ticker found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>URL:</b> {link}\n"
    message += f"<b>Ticker:</b> {ticker}\n"
    message += f"<b>Exchange:</b> {exchange}\n"
    message += f"<b>Post Date:</b> {date_gmt}\n"

    await send_ws_message(
        {
            "name": "Ningi Research",
            "type": "Sell",
            "ticker": ticker,
            "sender": "ningi",
        },
    )
    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Report sent to Telegram: {ticker} ({exchange}) - {link}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()

    while True:
        await sleep_until_market_open()
        await initialize_websocket()

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
            posts = await fetch_json()

            new_posts = []
            ticker_posts = []

            for post in posts:
                link = post.get("link")
                if not link or link in processed_urls:
                    continue

                title = post.get("title", {}).get("rendered", "")
                date_gmt = post.get("date_gmt", "")

                post_data = {"link": link, "title": title, "date_gmt": date_gmt}

                new_posts.append(post_data)

                ticker, exchange = extract_ticker_from_title(title)
                if ticker and exchange:
                    ticker_posts.append((post_data, ticker, exchange))

                processed_urls.add(link)

            if new_posts:
                log_message(f"Found {len(new_posts)} new posts to process.", "INFO")

                for post_data, ticker, exchange in ticker_posts:
                    await send_to_telegram(post_data, ticker, exchange)

                await send_posts_to_telegram(new_posts)
                save_processed_urls(processed_urls)
            else:
                log_message("No new posts found.", "INFO")

            await asyncio.sleep(CHECK_INTERVAL)


def main():
    global page, co
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        page.quit()
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        page.quit()
        sys.exit(1)


if __name__ == "__main__":
    main()
