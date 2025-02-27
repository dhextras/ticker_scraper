import asyncio
import json
import os
import re

import aiohttp
from bs4 import BeautifulSoup
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
REPORT_URL = "https://www.blueorcacapital.com/category/reports/"
CHECK_INTERVAL = 1  # seconds
PROCESSED_REPORTS_FILE = "data/blueorca_processed_reports.json"
TELEGRAM_BOT_TOKEN = os.getenv("BLUEORCA_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BLUEORCA_TELEGRAM_GRP")

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


async def fetch_reports(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(REPORT_URL, headers=headers) as response:
            if response.status == 200:
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")
                table = soup.select_one("#content > div > div > table")

                if not table:
                    log_message("Table not found on the page", "ERROR")
                    return []

                reports = []
                rows = table.select("tbody > tr")

                for row in rows:
                    cols = row.select("td")
                    if len(cols) >= 4:
                        link_element = cols[0].select_one("a")
                        if link_element:
                            url = link_element.get("href", "")
                            title = link_element.text.strip()
                            company = cols[1].text.strip()
                            ticker = cols[2].text.strip()
                            date = cols[3].text.strip()

                            reports.append(
                                {
                                    "url": url,
                                    "title": title,
                                    "company": company,
                                    "ticker": ticker,
                                    "date": date,
                                }
                            )

                log_message(f"Fetched {len(reports)} reports", "INFO")
                return reports
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent."
                    "WARNING",
                )
                return []
            else:
                log_message(f"Failed to fetch reports: HTTP {response.status}", "ERROR")
                return []
    except Exception as e:
        log_message(f"Error fetching reports: {e}", "ERROR")
        return []


def extract_ticker(ticker_text):
    # Extract ticker from format like "NYSE: TDOC" or "NASDAQ: GDS"
    ticker_pattern = r"(?:NYSE|NASDAQ|AMEX|ASX|KOSDAQ|HK):\s*([A-Z0-9]+)"
    match = re.search(ticker_pattern, ticker_text)
    if match:
        return match.group(1)
    return ticker_text


async def send_report_to_telegram(report):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
    ticker = extract_ticker(report["ticker"])

    message = f"<b>New Blue Orca Report</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Report:</b> {report['title']}\n"
    message += f"<b>Company:</b> {report['company']}\n"
    message += f"<b>Ticker:</b> {report['ticker']}\n"
    message += f"<b>Date:</b> {report['date']}\n"
    message += f"<b>URL:</b> {report['url']}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New report sent to Telegram: {report['title']} - {ticker}", "INFO")


async def run_report_monitor():
    processed_reports = load_processed_reports()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            log_message("Market is open. Starting to check for new reports...", "DEBUG")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking for new reports...")
                reports = await fetch_reports(session)

                for report in reports:
                    report_url = report["url"]
                    if report_url not in processed_reports:
                        log_message(f"Found new report: {report['title']}", "INFO")
                        await send_report_to_telegram(report)
                        processed_reports.add(report_url)

                if reports:
                    save_processed_reports(processed_reports)

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        return

    try:
        asyncio.run(run_report_monitor())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")


if __name__ == "__main__":
    main()
