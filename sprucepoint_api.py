import asyncio
import json
import os
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

load_dotenv()

# Constants
CURRENT_YEAR = datetime.now().year
API_URL = f"https://api.jetboost.io/search?boosterId=clmaxseess1tj0652a3dur1z6&q={CURRENT_YEAR}"
CHECK_INTERVAL = 0.5  # seconds
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


async def send_report_to_telegram(report_slug):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
    report_url = f"{REPORT_BASE_URL}{report_slug}"

    company_name = " ".join(word.capitalize() for word in report_slug.split("-"))

    message = f"<b>New Spruce Point Capital Report Detected</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>Company:</b> {company_name}\n"
    message += f"<b>URL:</b> {report_url}\n\n"
    message += "<i>Note: This alert is based on API detection. Visit the URL for full report details.</i>"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"New report detected via API and sent to Telegram: {company_name}", "INFO"
    )


async def run_api_monitor():
    processed_reports = load_processed_reports()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
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
                        log_message(f"Found new report via API: {slug}", "INFO")
                        await send_report_to_telegram(slug)
                        processed_reports.add(slug)

                        date = get_current_time().strftime("%Y_%m_%d_%H_%M_%S")
                        with open(
                            f"data/jetboost_detected_report_{date}.txt", "w"
                        ) as f:
                            f.write(
                                f"Detected new report: {slug}\nFull URL: {REPORT_BASE_URL}{slug}"
                            )

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
