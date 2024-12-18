import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from time import time

import bs4
import pytz
import requests
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("INVESTOR_PLACE_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("INVESTOR_PLACE_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
IPA_LOGIN_COOKIE = os.getenv("IPA_LOGIN_COOKIE")
CHECK_INTERVAL = 0.2  # seconds
DATA_DIR = Path("data")
ALERTS_FILE = DATA_DIR / "investorplace_protfolio_csv.json"
JSON_URL = "https://investorplace.com/acceleratedprofits/wp-json/wp/v2/pages/5738"

# Global variables to store previous alerts
previous_alerts = []


def load_saved_alerts():
    """Load previously saved alerts from disk"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        if ALERTS_FILE.exists():
            with open(ALERTS_FILE, "r") as f:
                data = json.load(f)
                return data
        return []
    except Exception as e:
        log_message(f"Error loading saved alerts: {e}", "ERROR")
        return []


def save_alerts(data):
    """Save alerts to disk"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(ALERTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log_message(f"Error saving alerts: {e}", "ERROR")


def extract_new_tickers(old_alerts, new_alerts):
    """
    Extract tickers that been either added or removed from old alerts
    Returns list of tuples: (action, ticker)
    """
    tickers = []

    try:
        old_symbols = [alert["symbol"] for alert in old_alerts]
        new_symbols = [alert["symbol"] for alert in new_alerts]

        tickers += [
            ("Buy", symbol) for symbol in new_symbols if symbol not in old_symbols
        ]
        tickers += [
            ("Sell", symbol) for symbol in old_symbols if symbol not in new_symbols
        ]

        return tickers
    except Exception as e:
        log_message(f"Error extracting tickers:\n{e}", "ERROR")
        return []


def process_raw_data(html):
    try:
        soup = bs4.BeautifulSoup(html, "html.parser")

        # Find all rows & Extarct values based on index
        rows = soup.find_all("tr", class_=re.compile(r"js-stock-\d+"))
        extracted_data = []
        for row in rows:
            columns = row.find_all("td")
            if columns:
                data = {
                    "rank": columns[0].get_text(strip=True),
                    "symbol": columns[1].find("a").get_text(strip=True),
                    "company": (
                        columns[1].find("small").get_text(strip=True)
                        if columns[1].find("small")
                        else ""
                    ),
                    "date": columns[2].get_text(strip=True),
                    "price1": columns[3].get_text(strip=True),
                    "price2": columns[4].get_text(strip=True),
                    "percentage_change": columns[5].get_text(strip=True),
                    "target_price": columns[6].get_text(strip=True),
                    "latest_update": (
                        columns[7].find("a").get("href") if columns[7].find("a") else ""
                    ),
                }
                extracted_data.append(data)

        return extracted_data
    except Exception as e:
        log_message(f"Failed to process raw html data:\n{e}", "ERROR")
        return []


async def fetch_csv_alerts(url):
    """Fetch and parse CSV data from InvestorPlace"""
    try:
        headers = {"Cookie": f"ipa_login={IPA_LOGIN_COOKIE}"}

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        if response.status_code == 200:
            data = response.json()
            return data.get("content", {}).get("rendered", None)

        return None

    except Exception as e:
        log_message(f"Error fetching CSV data: {e}", "ERROR")
        return None


async def process_alert():
    """Check for new alerts and send to Telegram if found"""
    global previous_alerts

    try:
        start = time()
        raw_html = await fetch_csv_alerts(JSON_URL)
        log_message(f"fetch_csv_alerts took {(time() - start):.2f} seconds")
        if raw_html is None:
            return

        csv_alerts = process_raw_data(raw_html)
        if not csv_alerts or len(csv_alerts) <= 0:
            return

        current_time = datetime.now(pytz.utc)

        tickers = extract_new_tickers(previous_alerts, csv_alerts)
        if tickers:
            for action, ticker in tickers:
                await send_ws_message(
                    {
                        "name": "Navallier Old CSV",
                        "type": action,
                        "ticker": ticker,
                        "sender": "navallier",
                    },
                    WS_SERVER_URL,
                )

            ticker_text = "\n".join(
                [f"- {action}: {ticker}" for action, ticker in tickers]
            )

            message = (
                f"<b>New InvestorPlace CSV Alert!</b>\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>Changed Tickers:</b>\n{ticker_text}"
            )

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            log_message(f"Sent new alerts to Telegram, Changed tickers:\n{ticker_text}")

            previous_alerts = csv_alerts
            save_alerts(previous_alerts)

    except Exception as e:
        log_message(f"Error checking alerts: {e}", "ERROR")


async def run_scraper():
    """Main scraper loop that respects market hours"""
    global previous_alerts
    previous_alerts = load_saved_alerts()

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new posts...")

        # Get market times
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))
            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...")
                break

            log_message("Checking for new alerts...")
            try:
                await process_alert()

            except Exception as e:
                log_message(f"Error in scraper loop: {e}", "ERROR")

            await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, IPA_LOGIN_COOKIE]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        save_alerts(previous_alerts)
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
