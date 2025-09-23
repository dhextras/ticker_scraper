import asyncio
import json
import os
import re
import sys
import time
from typing import Dict, List, Optional, Set
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
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

# Constants
ARCHIVE_ALERT_URL = (
    "https://oxfordclub.com/publications/oxford-microcap-trader/?archive=alert"
)
PORTFOLIO_URL = "https://oxfordclub.com/publications/oxford-microcap-trader/?archive=portfolio&portfolio_id=13690"
LOGIN_URL = "https://oxfordclub.com/wp-login.php"

# Environment variables
USERNAME = os.getenv("OXFORD_MICROCAP_USERNAME")
PASSWORD = os.getenv("OXFORD_MICROCAP_PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("OXFORDCLUB_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("OXFORDCLUB_TELEGRAM_GRP")

# Configuration
CHECK_INTERVAL = 1
PROCESSED_ALERTS_FILE = "data/oxford_microcap_processed_alerts.json"
PROCESSED_PORTFOLIO_FILE = "data/oxford_microcap_processed_portfolio.json"

os.makedirs("data", exist_ok=True)


class ProcessedDataManager:
    """Manages processed URLs and portfolio entries"""

    def __init__(self, alerts_file: str, portfolio_file: str):
        self.alerts_file = alerts_file
        self.portfolio_file = portfolio_file
        self.processed_alerts = self.load_processed_alerts()
        self.processed_portfolio = self.load_processed_portfolio()

    def load_processed_alerts(self) -> Set[str]:
        try:
            with open(self.alerts_file, "r") as f:
                return set(json.load(f))
        except FileNotFoundError:
            return set()

    def load_processed_portfolio(self) -> Set[str]:
        try:
            with open(self.portfolio_file, "r") as f:
                return set(json.load(f))
        except FileNotFoundError:
            return set()

    def save_processed_alerts(self):
        with open(self.alerts_file, "w") as f:
            json.dump(list(self.processed_alerts), f, indent=2)
        log_message("Processed alert URLs saved.", "INFO")

    def save_processed_portfolio(self):
        with open(self.portfolio_file, "w") as f:
            json.dump(list(self.processed_portfolio), f, indent=2)
        log_message("Processed portfolio entries saved.", "INFO")

    def add_alert(self, url: str):
        self.processed_alerts.add(url)

    def add_portfolio_entry(self, entry_key: str):
        self.processed_portfolio.add(entry_key)

    def is_alert_processed(self, url: str) -> bool:
        return url in self.processed_alerts

    def is_portfolio_processed(self, entry_key: str) -> bool:
        return entry_key in self.processed_portfolio


def login_sync(session: requests.Session) -> bool:
    """Login to Oxford Club"""
    try:
        payload = {"log": USERNAME, "pwd": PASSWORD}
        response = session.post(LOGIN_URL, data=payload)
        if response.status_code == 200:
            log_message("Login successful", "INFO")
            return True
        elif 500 <= response.status_code < 600:
            log_message(
                f"Server error {response.status_code}: Temporary issue, safe to ignore if infrequent.",
                "WARNING",
            )
            return False
        log_message(f"Login failed: HTTP {response.status_code}", "ERROR")
        return False
    except Exception as e:
        log_message(f"Error during login: {e}", "ERROR")
        return False


# FIXME: Remove this function after confirming there are no 429 errors
async def send_429_alert():
    """Send alert when hitting rate limits"""
    message = f"🚨 <b>Oxford Microcap Trader - Rate Limit Alert</b>\n\n"
    message += f"<b>Time:</b> {get_current_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
    message += f"<b>Issue:</b> Hit 429 rate limit while scraping\n"
    message += f"<b>Action:</b> Please check the scraper status"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def fetch_initial_content(session: requests.Session, url: str) -> Optional[str]:
    """Fetch the initial content URL from an article"""
    try:
        response = session.get(url)
        if response.status_code == 429:
            await send_429_alert()
            return None
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


async def fetch_article_content(
    session: requests.Session, url: str
) -> Optional[BeautifulSoup]:
    """Fetch article content and return BeautifulSoup object"""
    try:
        response = session.get(url)
        if response.status_code == 429:
            await send_429_alert()
            return None
        if response.status_code != 200:
            return None

        return BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        log_message(f"Error fetching article content: {e}", "ERROR")
        return None


def extract_ticker_from_text(soup: BeautifulSoup, url: str) -> Optional[str]:
    """Extract ticker from article content using improved regex"""
    try:
        all_text = soup.get_text(separator=" ", strip=True)
        action_sections = re.split(r"Action to Take", all_text, flags=re.IGNORECASE)

        if len(action_sections) < 2:
            log_message(f"'Action to Take' not found: {url}", "WARNING")
            return None

        for section in action_sections[1:]:
            buy_match = re.search(r"Buy", section, re.IGNORECASE)

            ticker_match = re.search(
                r"(?:NYSE|NASDAQ)\s*:\s*\(?\*?([A-Z]{1,5})\*?\)?",
                section,
                re.IGNORECASE,
            )

            ticker: str = ""
            if ticker_match:
                ticker = ticker_match.group(1)
            else:
                # Fallback: ticker only, no exchange
                ticker_match = re.search(
                    r"\(\s*([A-Z]{1,5})\s*\)",
                    section,
                    re.IGNORECASE,
                )
                if ticker_match:
                    ticker = ticker_match.group(1)

            if (
                buy_match
                and ticker
                and buy_match.start()
                < (ticker_match.start() if ticker_match else float("inf"))
            ):
                return ticker
            elif not ticker:
                log_message(f"No ticker found in section: {url}", "WARNING")
            elif not buy_match:
                log_message(f"'Buy' not found in section: {url}", "WARNING")

        return None
    except Exception as e:
        log_message(f"Error extracting ticker: {e}", "ERROR")
        return None


async def fetch_and_process_alerts(session: requests.Session) -> List[Dict[str, str]]:
    """Fetch and process alert archive"""
    cache_timestamp = int(time.time() * 10000)
    try:
        headers = {
            "Connection": "keep-alive",
            "cache-control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "cache-timestamp": str(cache_timestamp),
            "cache-uuid": str(uuid4()),
        }

        response = session.get(ARCHIVE_ALERT_URL, headers=headers)
        if response.status_code == 429:
            await send_429_alert()
            return []
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
        log_message(f"Error fetching alerts: {e}", "ERROR")
        return []


async def fetch_and_process_portfolio(
    session: requests.Session,
) -> List[Dict[str, str]]:
    """Fetch and process portfolio entries"""
    cache_timestamp = int(time.time() * 10000)
    try:
        headers = {
            "Connection": "keep-alive",
            "cache-control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "cache-timestamp": str(cache_timestamp),
            "cache-uuid": str(uuid4()),
        }

        response = session.get(PORTFOLIO_URL, headers=headers)
        if response.status_code == 429:
            await send_429_alert()
            return []
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, "html.parser")

        table = soup.find("table", class_="dataTable")
        if not table:
            log_message("Portfolio table not found", "WARNING")
            return []

        portfolio_entries = []
        rows = table.find("tbody").find_all("tr") if table.find("tbody") else []

        for row in rows:
            cells = row.find_all("td")
            if len(cells) >= 7:  # Ensure we have all required columns
                company = cells[0].get_text(strip=True)
                symbol = cells[1].get_text(strip=True)
                entry_date_cell = cells[2]
                entry_price = cells[3].get_text(strip=True)
                current_price = cells[4].get_text(strip=True)
                stop_price = cells[5].get_text(strip=True)
                comments = cells[6].get_text(strip=True)

                # Extract entry date and URL
                entry_link = entry_date_cell.find("a")
                entry_date = (
                    entry_link.get_text(strip=True)
                    if entry_link
                    else entry_date_cell.get_text(strip=True)
                )
                article_url = entry_link["href"] if entry_link else ""

                entry_key = f"{symbol}_{entry_date}_{article_url}"

                portfolio_entries.append(
                    {
                        "company": company,
                        "symbol": symbol,
                        "entry_date": entry_date,
                        "entry_price": entry_price,
                        "current_price": current_price,
                        "stop_price": stop_price,
                        "comments": comments,
                        "article_url": article_url,
                        "entry_key": entry_key,
                    }
                )

        return portfolio_entries
    except Exception as e:
        log_message(f"Error fetching portfolio: {e}", "ERROR")
        return []


async def send_alert_to_telegram_and_ws(
    article_data: Dict[str, str], content_url: str, ticker: str
):
    """Send alert notification to Telegram and WebSocket"""
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")

    message = f"<b>New Oxford Microcap Trader Alert Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Issue URL:</b> {article_data['url']}\n"
    message += f"<b>Actual Content URL:</b> {content_url}\n"
    message += f"<b>Title:</b> {article_data['title']}\n"

    if ticker:
        message += f"\n<b>Extracted Ticker:</b> {ticker}"

        await send_ws_message(
            {
                "name": "Oxford - MTA",
                "type": "Buy",
                "ticker": ticker,
                "sender": "oxfordclub",
            },
        )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def send_portfolio_to_telegram_and_ws(portfolio_entry: Dict[str, str]):
    """Send portfolio notification to Telegram and WebSocket"""
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")

    message = f"<b>New Oxford Microcap Trader Portfolio Entry</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Company:</b> {portfolio_entry['company']}\n"
    message += f"<b>Symbol:</b> {portfolio_entry['symbol']}\n"
    message += f"<b>Entry Date:</b> {portfolio_entry['entry_date']}\n"
    message += f"<b>Entry Price:</b> {portfolio_entry['entry_price']}\n"
    message += f"<b>Current Price:</b> {portfolio_entry['current_price']}\n"
    message += f"<b>Stop Price:</b> {portfolio_entry['stop_price']}\n"
    message += f"<b>Comments:</b> {portfolio_entry['comments']}\n"

    if portfolio_entry["article_url"]:
        message += f"<b>Article URL:</b> {portfolio_entry['article_url']}\n"

    await send_ws_message(
        {
            "name": "Oxford - MTP",
            "type": "Buy",
            "ticker": portfolio_entry["symbol"],
            "sender": "oxfordclub",
        },
    )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def process_alerts(
    session: requests.Session, data_manager: ProcessedDataManager
) -> int:
    """Process alert articles and return count of new articles"""
    articles = await fetch_and_process_alerts(session)
    new_articles = [
        article
        for article in articles
        if not data_manager.is_alert_processed(article["url"])
    ]

    for article in new_articles:
        initial_url = await fetch_initial_content(session, article["url"])
        if initial_url:
            content_soup = await fetch_article_content(session, initial_url)
            if content_soup:
                ticker = extract_ticker_from_text(content_soup, initial_url)
                if ticker:
                    await send_alert_to_telegram_and_ws(article, initial_url, ticker)
                    data_manager.add_alert(article["url"])
        else:
            log_message(
                f"Couldn't find the inside content URL for: {article['url']}", "ERROR"
            )

    return len(new_articles)


async def process_portfolio(
    session: requests.Session, data_manager: ProcessedDataManager
) -> int:
    """Process portfolio entries and return count of new entries"""
    portfolio_entries = await fetch_and_process_portfolio(session)
    new_entries = [
        entry
        for entry in portfolio_entries
        if not data_manager.is_portfolio_processed(entry["entry_key"])
    ]

    for entry in new_entries:
        await send_portfolio_to_telegram_and_ws(entry)
        data_manager.add_portfolio_entry(entry["entry_key"])

    return len(new_entries)


async def run_alerts_scraper(
    data_manager: ProcessedDataManager, stop_event: asyncio.Event
):
    """Run the alerts scraper"""
    session = requests.Session()

    if not login_sync(session):
        log_message("Failed to login for alerts scraper", "ERROR")
        return

    log_message("Alerts scraper started", "INFO")

    while not stop_event.is_set():
        try:
            new_alerts_count = await process_alerts(session, data_manager)

            if new_alerts_count > 0:
                data_manager.save_processed_alerts()
                log_message(f"Processed {new_alerts_count} new alert articles.", "INFO")
            else:
                log_message("No new alert articles found.", "INFO")
        except Exception as e:
            log_message(f"Error in alerts processing: {e}", "ERROR")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass

    log_message("Alerts scraper stopped", "INFO")


async def run_portfolio_scraper(
    data_manager: ProcessedDataManager, stop_event: asyncio.Event
):
    """Run the portfolio scraper"""
    session = requests.Session()

    if not login_sync(session):
        log_message("Failed to login for portfolio scraper", "ERROR")
        return

    log_message("Portfolio scraper started", "INFO")

    while not stop_event.is_set():
        try:
            new_entries_count = await process_portfolio(session, data_manager)

            if new_entries_count > 0:
                data_manager.save_processed_portfolio()
                log_message(
                    f"Processed {new_entries_count} new portfolio entries.", "INFO"
                )
            else:
                log_message("No new portfolio entries found.", "INFO")
        except Exception as e:
            log_message(f"Error in portfolio processing: {e}", "ERROR")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass

    log_message("Portfolio scraper stopped", "INFO")


async def run_scraper():
    """Main scraper function that handles market timing and coordinates scrapers"""
    data_manager = ProcessedDataManager(PROCESSED_ALERTS_FILE, PROCESSED_PORTFOLIO_FILE)

    while True:
        await sleep_until_market_open()
        await initialize_websocket()
        log_message("Market is open. Starting scrapers...", "DEBUG")

        _, _, market_close_time = get_next_market_times()
        stop_event = asyncio.Event()

        alerts_task = asyncio.create_task(run_alerts_scraper(data_manager, stop_event))
        portfolio_task = asyncio.create_task(
            run_portfolio_scraper(data_manager, stop_event)
        )

        while True:
            current_time = get_current_time()
            if current_time > market_close_time:
                log_message("Market is closed. Stopping scrapers...", "DEBUG")
                stop_event.set()
                break

            await asyncio.sleep(10)

        await asyncio.gather(alerts_task, portfolio_task, return_exceptions=True)
        log_message("Both scrapers stopped. Waiting for next market open...", "INFO")


def main():
    """Main entry point"""
    if not all([USERNAME, PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
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
