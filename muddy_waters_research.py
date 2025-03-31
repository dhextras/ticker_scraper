import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Dict, Optional
from uuid import uuid4

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.bypass_cloudflare import bypasser
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()

# Constants
RESEARCH_URL = "https://muddywatersresearch.com/research/"
JSON_URL = "https://www.muddywatersresearch.com/wp-json/wp/v2/media"
CHECK_INTERVAL = 3  # seconds
PROCESSED_REPORTS_FILE = "data/muddy_waters_processed_reports.json"
SESSION_FILE = "data/muddy_waters_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("MUDDY_WATERS_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("MUDDY_WATERS_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_cookies(fresh=False) -> Optional[Dict[str, Any]]:
    try:
        cookies = None
        if fresh == False:
            if not os.path.exists(SESSION_FILE):
                log_message(f"Session file not found: {SESSION_FILE}", "WARNING")
            else:
                with open(SESSION_FILE, "r") as f:
                    cookies = json.load(f)

        if not cookies or cookies.get("cf_clearance", "") == "":
            log_message(
                "Invalid or missing 'cf_clearance' in cookies. Attempting to regenerate.",
                "WARNING",
            )
            bypass = bypasser(JSON_URL, SESSION_FILE)

            if not bypass or bypass == False:
                return

            with open(SESSION_FILE, "r") as f:
                cookies = json.load(f)

            if not cookies or cookies.get("cf_clearance", "") == "":
                return None

        return cookies

    except json.JSONDecodeError:
        log_message("Failed to decode JSON from session file.", "ERROR")
    except Exception as e:
        log_message(f"Error loading session: {e}", "ERROR")

    return None


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


async def fetch_reports(session, cookies):
    timestamp = int(time.time() * 10000)
    cache_uuid = uuid4()

    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "cache-control": "no-cache, no-store, max-age=0, must-revalidate, private",
            "pragma": "no-cache",
            "Cookie": f"cf_clearance:{cookies['cf_clearance']}",
            "cache-timestamp": str(timestamp),
            "cache-uuid": str(cache_uuid),
        }

        url = f"{RESEARCH_URL}?cache-timestamp={timestamp}"

        async with session.get(url, headers=headers, cookies=cookies) as response:
            if response.status == 200:
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")

                table = soup.select_one("#research-table")

                if not table:
                    log_message("Research table not found on the page", "ERROR")
                    return [], None

                reports = []

                # Parse rows
                rows = table.select("tbody > tr")

                for row in rows:
                    try:
                        title_cell = row.select_one("td.first")
                        company_cell = row.select_one("td.mid")
                        date_cell = row.select_one("td.last")

                        if title_cell and company_cell and date_cell:
                            link_element = title_cell.select_one("a")

                            if link_element:
                                url = link_element.get("href", "")
                                title = link_element.text.strip()

                                is_new = bool(
                                    title_cell.select_one(".reports-table__new-tag")
                                )

                                company_link = company_cell.select_one("a")
                                company = (
                                    company_link.text.strip()
                                    if company_link
                                    else company_cell.text.strip()
                                )

                                date = date_cell.text.strip()

                                ticker_match = re.search(
                                    r"\(([A-Z]+)\s*(?:US)?\)", title
                                )
                                ticker = ticker_match.group(1) if ticker_match else ""

                                reports.append(
                                    {
                                        "url": url,
                                        "title": title,
                                        "company": company,
                                        "date": date,
                                        "is_new": is_new,
                                        "ticker": ticker,
                                    }
                                )
                    except Exception as e:
                        log_message(f"Error parsing row: {e}", "ERROR")

                log_message(
                    f"Fetched {len(reports)} reports from research page", "INFO"
                )
                return reports, None

            elif response.status == 403:
                log_message(
                    "Cloudflare clearance expired, attempting to refresh", "WARNING"
                )
                cookies = load_cookies(fresh=True)
                if not cookies:
                    raise Exception("CF_CLEARANCE Failed")
                return [], cookies

            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return [], None

            else:
                log_message(f"Failed to fetch reports: HTTP {response.status}", "ERROR")
                return [], None

    except Exception as e:
        if "CF_CLEARANCE Failed" in str(e):
            raise
        log_message(f"Error fetching reports: {e}", "ERROR")
        return [], None


async def send_report_to_telegram(report):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Muddy Waters Research Report</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Report:</b> {report['title']}\n"
    message += f"<b>Company:</b> {report['company']}\n"

    if report["ticker"]:
        message += f"<b>Ticker:</b> {report['ticker']}\n"

    message += f"<b>Date:</b> {report['date']}\n"
    message += f"<b>URL:</b> {report['url']}\n"

    if report["is_new"]:
        message += "\n<b>⚠️ Marked as NEW on the website!</b>"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New report sent to Telegram: {report['title']}", "INFO")


async def run_report_monitor():
    processed_reports = load_processed_reports()
    cookies = load_cookies()

    if not cookies:
        log_message("Failed to get valid cf_clearance", "CRITICAL")
        return

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
                reports, new_cookies = await fetch_reports(session, cookies)

                cookies = new_cookies if new_cookies is not None else cookies

                if not reports:
                    log_message(
                        "Failed to fetch reports or no reports found", "WARNING"
                    )
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                new_reports_found = False
                for report in reports:
                    report_url = report["url"]
                    if report_url not in processed_reports:
                        log_message(f"Found new report: {report['title']}", "INFO")
                        await send_report_to_telegram(report)
                        processed_reports.add(report_url)
                        new_reports_found = True

                if new_reports_found:
                    save_processed_reports(processed_reports)

                else:
                    log_message("No new reports found.", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_report_monitor())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
