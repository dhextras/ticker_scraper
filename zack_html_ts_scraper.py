import asyncio
import json
import os
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
TELEGRAM_BOT_TOKEN = os.getenv("ZACKS_TS_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("ZACKS_TS_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
ZACKS_USERNAME = os.getenv("ZACKS_USERNAME")
ZACKS_PASSWORD = os.getenv("ZACKS_PASSWORD")
CHECK_INTERVAL = 0.2  # seconds
DATA_DIR = Path("data")
ALERTS_FILE = DATA_DIR / "zacks_ts_portfolio.json"
ZACKS_URL = "https://www.zacks.com/tradingservices/index.php"

# Global variables to store previous alerts
previous_alerts = []


def load_saved_alerts():
    """Load previously saved alerts from disk"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        if ALERTS_FILE.exists():
            with open(ALERTS_FILE, "r") as f:
                return json.load(f)
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


def create_session(force_login=False):
    """Create and return a new session with login if needed"""
    try:
        session = requests.Session()
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.zacks.com",
            "referer": "https://www.zacks.com/tradingservices/index.php?ts_id=18&newsletterid=268",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.37 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        params = {
            "ts_id": "18",
            "newsletterid": "268",
        }

        if force_login:
            data = {
                "force_login": "true",
                "username": ZACKS_USERNAME,
                "password": ZACKS_PASSWORD,
                "remember_me": "on",
            }

            response = session.post(
                ZACKS_URL,
                params=params,
                headers=headers,
                data=data,
            )

            if response.status_code != 200:
                raise Exception(
                    f"Login failed with status code: {response.status_code}"
                )

        return session
    except Exception as e:
        log_message(f"Error creating session: {e}", "ERROR")
        return None


def extract_new_tickers(old_alerts, new_alerts):
    """
    Extract tickers that have been either added or removed from old alerts
    Returns list of tuples: (action, ticker)
    """
    try:
        old_symbols = [alert["symbol"] for alert in old_alerts]
        new_symbols = [alert["symbol"] for alert in new_alerts]

        tickers = []
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
    """Process HTML and extract portfolio data"""
    try:
        soup = bs4.BeautifulSoup(html, "html.parser")
        table = soup.find("table", id="port_sort")
        if not table:
            return []

        tbody = table.find("tbody")
        if not tbody:
            return []

        rows = tbody.find_all("tr")
        extracted_data = []

        for row in rows:
            try:
                symbol_elem = row.find("td", class_="symbol")
                if not symbol_elem:
                    continue

                symbol_list = symbol_elem.find("a", class_="hoverquote-container-od")[
                    "rel"
                ]

                if len(symbol_list) < 1:
                    continue

                data = {
                    "company": row.find("th", class_="company")["title"],
                    "symbol": symbol_list[0],
                    "value_percent": row.find(
                        "td", class_="value-percent"
                    ).text.strip(),
                    "date_added": row.find("td", class_="date-add").text.strip(),
                    "type": row.find("td", class_="type").text.strip(),
                    "price_added": row.find("td", class_="price-add").text.strip(),
                    "price_last": row.find("td", class_="price-last").text.strip(),
                    "change_percent": row.find(
                        "td", class_="change-percent"
                    ).text.strip(),
                }
                extracted_data.append(data)
            except Exception as e:
                log_message(f"Error processing row: {e}", "ERROR")
                continue

        return extracted_data
    except Exception as e:
        log_message(f"Failed to process raw html data:\n{e}", "ERROR")
        return []


async def fetch_portfolio_data(session):
    """Fetch portfolio data from Zacks"""
    try:
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.37 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        params = {
            "ts_id": "18",
            "newsletterid": "268",
        }

        response = session.get(ZACKS_URL, headers=headers, params=params)

        if response.status_code != 200:
            # Try force login if request fails
            new_session = create_session(force_login=True)

            if new_session:
                response = new_session.get(ZACKS_URL, headers=headers, params=params)
                return (
                    response.text if response.status_code == 200 else None
                ), new_session
            return None, session

        return response.text, session

    except Exception as e:
        log_message(f"Error fetching portfolio data: {e}", "ERROR")
        return None, session


async def process_alert(session):
    """Check for new alerts and send to Telegram if found"""
    global previous_alerts

    try:
        start = time()
        raw_html, session = await fetch_portfolio_data(session)
        log_message(f"fetch_portfolio_data took {(time() - start):.2f} seconds")

        if raw_html is None:
            return None

        portfolio_alerts = process_raw_data(raw_html)
        if not portfolio_alerts:
            return None

        current_time = datetime.now(pytz.utc)

        tickers = extract_new_tickers(previous_alerts, portfolio_alerts)
        if tickers:
            for action, ticker in tickers:
                await send_ws_message(
                    {
                        "name": "Zacks TS - counter strike",
                        "type": action,
                        "ticker": ticker,
                        "sender": "zacks",
                    },
                    WS_SERVER_URL,
                )

            ticker_text = "\n".join(
                [f"- {action}: {ticker}" for action, ticker in tickers]
            )

            message = (
                f"<b>New Zacks Trading Service Alert!</b>\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>Changed Tickers:</b>\n{ticker_text}"
            )

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            log_message(f"Sent new alerts to Telegram, Changed tickers:\n{ticker_text}")

            previous_alerts = portfolio_alerts
            save_alerts(previous_alerts)

        return session

    except Exception as e:
        log_message(f"Error checking alerts: {e}", "ERROR")
        return None


async def run_scraper():
    """Main scraper loop that respects market hours"""
    global previous_alerts

    previous_alerts = load_saved_alerts()
    session = create_session(force_login=True)

    if not session:
        log_message("Failed to initialize session", "CRITICAL")
        return

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
                session = await process_alert(session)
                if not session:
                    session = create_session(force_login=True)
                    if not session:
                        break
            except Exception as e:
                log_message(f"Error in scraper loop: {e}", "ERROR")

            await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ZACKS_USERNAME, ZACKS_PASSWORD]):
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
