import asyncio
import json
import os
import re
import time
from datetime import datetime

import aiohttp
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
CURRENT_YEAR = datetime.now().year
API_URL = f"https://api.jetboost.io/search?boosterId=clmaxseess1tj0652a3dur1z6&q={CURRENT_YEAR}"
CHECK_INTERVAL = 0.3  # seconds
PROCESSED_REPORTS_FILE = "data/jetboost_processed_reports.json"
REPORT_BASE_URL = "https://www.sprucepointcap.com/research/"
TELEGRAM_BOT_TOKEN = os.getenv("SPRUCEPOINT_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("SPRUCEPOINT_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_processed_reports():
    try:
        with open(PROCESSED_REPORTS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_reports(reports):
    with open(PROCESSED_REPORTS_FILE, "w") as f:
        json.dump(list(reports), f, indent=2)
    log_message("Processed reports saved.", "INFO")


async def fetch_reports_from_api(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }

        async with session.get(API_URL, headers=headers) as response:
            if response.status == 200:
                data = await response.json()

                report_slugs = [slug for slug, value in data.items() if value is True]

                log_message(
                    f"Fetched {len(report_slugs)} reports from JetBoost API", "INFO"
                )
                return report_slugs
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return []
            else:
                log_message(
                    f"Failed to fetch reports from API: HTTP {response.status}", "ERROR"
                )
                return []
    except Exception as e:
        log_message(f"Error fetching reports from API: {e}", "ERROR")
        return []


async def fetch_report_content(session, report_slug):
    """Fetch the HTML content of the report page to extract ticker information."""
    try:
        report_url = f"{REPORT_BASE_URL}{report_slug}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(report_url, headers=headers) as response:
            if response.status == 200:
                content = await response.text()
                return content
            else:
                log_message(
                    f"Failed to fetch report content: HTTP {response.status}", "ERROR"
                )
                return None
    except Exception as e:
        log_message(f"Error fetching report content: {e}", "ERROR")
        return None


def extract_ticker(html_content):
    """Extract the first stock ticker mentioned in the format (NYSE: ABC) or (NASDAQ: ABC)."""
    if not html_content:
        return None

    pattern = r"\((?:NYSE|NASDAQ|Nasdaq|Nyse):\s*([A-Z]+)\)"
    matches = re.findall(pattern, html_content, re.IGNORECASE)

    if matches:
        return matches[0]
    return None


async def send_report_to_telegram(report_slug, ticker=None, fetch_time=None):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
    report_url = f"{REPORT_BASE_URL}{report_slug}"

    company_name = " ".join(word.capitalize() for word in report_slug.split("-"))

    message = f"<b>New report detected - Research API</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>Article Fetch Time:</b> {fetch_time}\n"
    message += f"<b>Company:</b> {company_name}\n"

    if ticker:
        message += f"<b>Ticker:</b> {ticker}\n"

    message += f"<b>URL:</b> {report_url}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"New report detected via API and sent to Telegram: {company_name}{' (' + ticker + ')' if ticker else ''}",
        "INFO",
    )


async def process_new_report(session, slug):
    """Process a newly detected report."""
    log_message(f"Processing new report: {slug}", "INFO")

    start_fetch = time.time()
    html_content = await fetch_report_content(session, slug)

    # FIX: Remove this after finding out why it wasn't working
    date = get_current_time().strftime("%Y_%m_%d_%H_%M_%S")
    with open(f"data/sprucepoint_api_remove_{date}.html", "w") as f:
        f.write(str(html_content))

    ticker = extract_ticker(html_content)
    fetch_time = time.time() - start_fetch

    if ticker:
        await send_ws_message(
            {
                "name": "SpruePoint - Research API",
                "type": "Sell",
                "ticker": ticker,
                "sender": "sprucepoint",
                "target": "CSS",
            }
        )
        log_message(f"WebSocket message sent for ticker: {ticker}", "INFO")

    await send_report_to_telegram(slug, ticker, fetch_time)


async def run_api_monitor():
    processed_reports = load_processed_reports()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message(
                "Market is open. Starting to check API for new reports...", "DEBUG"
            )
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking JetBoost API for new reports...")
                report_slugs = await fetch_reports_from_api(session)

                for slug in report_slugs:
                    if slug not in processed_reports:
                        await process_new_report(session, slug)
                        processed_reports.add(slug)

                if report_slugs:
                    save_processed_reports(processed_reports)

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        return

    try:
        asyncio.run(run_api_monitor())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")


if __name__ == "__main__":
    main()
