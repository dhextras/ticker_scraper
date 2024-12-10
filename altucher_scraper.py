import asyncio
import json
import os
import re
import sys
from datetime import datetime

import pytz
import requests
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
JSON_URL = "https://my.paradigmpressgroup.com/api/article"
LOGIN_URL = "https://my.paradigmpressgroup.com/api/auth"
USERNAME = os.getenv("ALTUCHER_USERNAME")
PASSWORD = os.getenv("ALTUCHER_PASSWORD")
COOKIE_TID = os.getenv("ALTUCHER_COOKIE_TID")
COOKIE_ID = os.getenv("ALTUCHER_COOKIE_ID")
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/paradigm_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("ALTUCHER_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("ALTUCHER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

subscriptions = [
    {"name": "mm2", "id": "2rcJUw40n0QEtHPmYrdeeT"},
    {"name": "sei", "id": "32p68JKA43P2tQ0ibAeyDM"},
]

os.makedirs("data", exist_ok=True)


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


def login(session):
    try:
        payload = {"username": USERNAME, "password": PASSWORD}
        response = session.post(LOGIN_URL, data=payload)
        if response.status_code == 200:
            log_message("Login successful", "INFO")
            return session
        else:
            log_message(f"Login failed: HTTP {response.status_code}", "ERROR")
            return None
    except Exception as e:
        log_message(f"Error during login: {e}", "ERROR")
        return None


async def fetch_articles(session, subscription_name, subscription_id):
    try:
        params = {
            "include": 2,
            "order": "-fields.postDate",
            "fields.articleCategory.sys.id": "630ga2Gfm1hh4L2mHMkBHS",
            "fields.subscription.sys.id": subscription_id,
            "fields.postDate[gte]": "1999-12-31T18:00:00.000Z",
            "fields.postDate[lte]": "2030-12-31T10:22:51.880Z",
            "skip": 0,
            "limit": 10,
        }

        headers = {
            "Accept": "application/json",
            "Cookie": f"tid={COOKIE_TID}; _dd_s=logs=1&iid={COOKIE_ID}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        }

        response = session.get(JSON_URL, params=params, headers=headers)
        if response.status_code == 200:
            data = response.json()
            log_message(f"Fetched {len(data)} articles", "INFO")
            return data
        else:
            log_message(
                f"Failed to fetch {subscription_name} articles: HTTP {response.status_code}",
                "ERROR",
            )
            return []
    except Exception as e:
        log_message(f"Error fetching {subscription_name} articles: {e}", "ERROR")
        return []


async def process_articles(articles):
    buy_recommendations = []
    for article in articles:
        if article["title"].lower().startswith("buy alert:"):
            for stock_rec in article.get("stockRecommendations", []):
                if stock_rec["action"].lower() == "buy":
                    buy_recommendations.append(
                        {
                            "ticker": stock_rec["tickerSymbol"],
                            "name": stock_rec["stockName"],
                            "actionDesc": stock_rec["actionDescription"],
                            "postDate": article["cfUpdatedAt"],
                            "url": article["slug"],
                        }
                    )

    return buy_recommendations


async def send_matches_to_telegram(buy_recs):
    for rec in buy_recs:
        ticker = rec["ticker"]
        clean_ticker = re.match(r"^[A-Z]+", ticker)
        if clean_ticker:
            ticker = clean_ticker.group(0)

        name = rec["name"]
        actionDesc = rec["actionDesc"]
        postDate = rec["postDate"]
        url = f"https://my.paradigmpressgroup.com/article/{rec['url']}"

        message = f"<b>New Buy Recommendation</b>\n\n"
        message += f"<b>Stock Symbol:</b> {ticker}\n"
        message += f"<b>Stock Name:</b> {name}\n"
        message += f"<b>Action Desc:</b> {actionDesc}\n"
        message += f"<b>URL:</b> {url}\n"
        message += f"<b>Post Time:</b> {postDate}\n"

        await send_ws_message(
            {
                "name": "Altucher",
                "type": "Buy",
                "ticker": ticker,
                "sender": "altucher",
            },
            WS_SERVER_URL,
        )
        await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
        log_message(
            f"Recommendations sent to Telegram and WebSocket: {ticker} - {url}", "INFO"
        )


async def run_scraper():
    processed_urls = load_processed_urls()

    session = requests.Session()
    session = login(session)

    if not session:
        return

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new articles...")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))

            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...")
                break

            articles = []
            for subscription in subscriptions:
                articles += await fetch_articles(
                    session, subscription["name"], subscription["id"]
                )

            new_articles = [
                article
                for article in articles
                if article.get("slug") and article["slug"] not in processed_urls
            ]
            new_urls = [article["slug"] for article in articles]

            if new_articles:
                log_message(
                    f"Found {len(new_articles)} new articles to process.", "INFO"
                )

                buy_recs = await process_articles(new_articles)
                await send_matches_to_telegram(buy_recs)

                processed_urls.update(new_urls)
                save_processed_urls(new_urls)
            else:
                log_message("No new articles found.", "INFO")

            await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([USERNAME, PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP, WS_SERVER_URL]):
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
