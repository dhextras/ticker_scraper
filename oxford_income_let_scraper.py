import asyncio
import json
import os
import re
import sys
import time
from uuid import uuid4

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
ARCHIVE_URL = "https://oxfordclub.com/publications/income-letter/?archive=update"
LOGIN_URL = "https://oxfordclub.com/wp-login.php"
USERNAME = os.getenv("OXFORDCLUB_USERNAME")
PASSWORD = os.getenv("OXFORDCLUB_PASSWORD")
CHECK_INTERVAL = 1
PROCESSED_URLS_FILE = "data/oxford_income_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("OXFORDCLUB_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("OXFORDCLUB_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

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


def login_sync(session):
    try:
        payload = {"log": USERNAME, "pwd": PASSWORD}
        response = session.post(LOGIN_URL, data=payload)
        if response.status_code == 200:
            log_message("Login successful", "INFO")
            return True
        log_message(f"Login failed: HTTP {response.status_code}", "ERROR")
        return False
    except Exception as e:
        log_message(f"Error during login: {e}", "ERROR")
        return False


async def fetch_initial_content(session, url):
    try:
        response = session.get(url)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        link = soup.select_one(
            "body > div.page-section.members-content > div > article > div > p:nth-child(2) > a"
        )
        return link["href"] if link else None
    except Exception as e:
        log_message(f"Error fetching initial content: {e}", "ERROR")
        return None


async def fetch_article_content(session, url):
    try:
        response = session.get(url)
        if response.status_code != 200:
            return None
        return BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        log_message(f"Error fetching article content: {e}", "ERROR")
        return None


async def fetch_and_process_archive(session):
    cache_timestamp = int(time.time() * 10000)
    try:
        headers = {
            "Connection": "keep-alive",
            "cache-control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "cache-timestamp": str(cache_timestamp),
            "cache-uuid": str(uuid4()),
        }

        response = session.get(ARCHIVE_URL, headers=headers)
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        articles = soup.select(
            "body > div.page-section.members-content > div > div > a"
        )

        new_issue_articles = [
            {"url": article["href"], "title": article.get_text().strip()}
            for article in articles
            if "New Issue:" in article.get_text()
        ]

        return new_issue_articles
    except Exception as e:
        log_message(f"Error fetching archive: {e}", "ERROR")
        return []


async def send_to_telegram_and_ws(article_data, url, ticker):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")

    message = f"<b>New Income Letter Article Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Issue URL:</b> {article_data['url']}\n"
    message += f"<b>Actual Content URL:</b> {url}\n"
    message += f"<b>Title:</b> {article_data['title']}\n"

    if ticker:
        message += f"\n<b>Extracted Ticker:</b> {ticker}"

        await send_ws_message(
            {
                "name": "OXFORD Income Letter",
                "type": "Alert",
                "ticker": ticker,
                "sender": "oxfordclub",
                "traget": "CSS",
            },
            WS_SERVER_URL,
        )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


def extract_ticker_from_text(soup, url):
    try:
        all_text = soup.get_text(separator=" ", strip=True)
        action_sections = re.split(r"Action to Take", all_text, flags=re.IGNORECASE)

        if len(action_sections) < 2:
            log_message(f"'Action to Take' not found: {url}", "WARNING")
            return None

        for section in action_sections[1:]:
            buy_match = re.search(r"Buy", section, re.IGNORECASE)
            ticker_match = re.search(r"(NYSE|NASDAQ):\s*(\w+)", section, re.IGNORECASE)

            if buy_match and ticker_match and buy_match.start() < ticker_match.start():
                return ticker_match.group(2)
            elif not ticker_match:
                log_message(f"No ticker found in section: {url}", "WARNING")
            elif not buy_match or (
                buy_match and ticker_match and buy_match.start() > ticker_match.start()
            ):
                log_message(
                    f"'Buy' not found before ticker in section: {url}", "WARNING"
                )

        return None
    except Exception as e:
        log_message(f"Error extracting ticker: {e}", "ERROR")
        return None


async def run_scraper():
    processed_urls = load_processed_urls()
    session = requests.Session()

    if not login_sync(session):
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

            articles = await fetch_and_process_archive(session)
            new_articles = [
                article for article in articles if article["url"] not in processed_urls
            ]

            for article in new_articles:
                initial_url = await fetch_initial_content(session, article["url"])
                if initial_url:
                    content_soup = await fetch_article_content(session, initial_url)
                    if content_soup:
                        ticker = extract_ticker_from_text(content_soup, initial_url)
                        if ticker:
                            await send_to_telegram_and_ws(article, initial_url, ticker)
                            processed_urls.add(article["url"])
                else:
                    log_message(
                        f"Couldn't able to find the inside content url for: {article['url']}",
                        "ERROR",
                    )

            if new_articles:
                save_processed_urls(processed_urls)
                log_message(f"Processed {len(new_articles)} new articles.", "INFO")
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
