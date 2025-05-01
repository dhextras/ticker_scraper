import asyncio
import json
import os

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
REPORT_URL = "https://www.sprucepointcap.com/research"
CHECK_INTERVAL = 0.5  # seconds
PROCESSED_REPORTS_FILE = "data/sprucepoint_processed_reports.json"
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


async def fetch_reports(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(REPORT_URL, headers=headers) as response:
            if response.status == 200:
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")

                research_items_container = soup.select_one(
                    "div.jetboost-list-wrapper-1dlp div.w-dyn-items"
                )

                if not research_items_container:
                    log_message(
                        "Research items container not found on the page", "ERROR"
                    )
                    return []

                reports = []

                research_items = research_items_container.select("div.research-wrap")

                for item in research_items:
                    url_element = item.select_one("a[href^='/research/']")
                    if not url_element:
                        continue

                    relative_url = url_element.get("href", "")
                    full_url = f"https://www.sprucepointcap.com{relative_url}"

                    date_element = item.select_one("div.research-date")
                    date = date_element.text.strip() if date_element else "Unknown"

                    title_element = item.select_one("h3.research-h3")
                    title = title_element.text.strip() if title_element else "Unknown"

                    ticker_element = item.select_one(
                        "div.stock-designation div.uppercase"
                    )
                    ticker = (
                        ticker_element.text.strip() if ticker_element else "Unknown"
                    )

                    company = title

                    position_element = item.select_one(
                        "div.options-wrapper div.industry-position:nth-child(3) div:nth-child(2)"
                    )
                    position = (
                        position_element.text.strip() if position_element else "Unknown"
                    )

                    sector_element = item.select_one(
                        "div.options-wrapper div.industry-position:nth-child(2) div.font-size-13px"
                    )
                    sector = (
                        sector_element.text.strip() if sector_element else "Unknown"
                    )

                    index_element = item.select_one(
                        "div.options-wrapper div.industry-position:nth-child(1) div:nth-child(2)"
                    )
                    index = index_element.text.strip() if index_element else "Unknown"

                    reports.append(
                        {
                            "url": full_url,
                            "title": title,
                            "company": company,
                            "ticker": ticker,
                            "date": date,
                            "position": position,
                            "sector": sector,
                            "index": index,
                        }
                    )

                log_message(f"Fetched {len(reports)} reports", "INFO")
                return reports
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return []
            else:
                log_message(f"Failed to fetch reports: HTTP {response.status}", "ERROR")
                return []
    except Exception as e:
        log_message(f"Error fetching reports: {e}", "ERROR")
        return []


async def send_report_to_telegram(report):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Spruce Point Capital Report</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>Report:</b> {report['title']}\n"
    message += f"<b>Company:</b> {report['company']}\n"
    message += f"<b>Ticker:</b> {report['ticker']}\n"
    message += f"<b>Date:</b> {report['date']}\n"
    message += f"<b>Position:</b> {report['position']}\n"
    message += f"<b>Sector:</b> {report['sector']}\n"
    message += f"<b>Index:</b> {report['index']}\n"
    message += f"<b>URL:</b> {report['url']}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"New report sent to Telegram: {report['title']} - {report['ticker']}", "INFO"
    )


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
