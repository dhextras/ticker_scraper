import asyncio
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime

import aiohttp
import pytz
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
HTML_URL = "https://www.jcapitalresearch.com/company-reports.html"
CHECK_INTERVAL = 5  # seconds
PROCESSED_REPORTS_FILE = "data/jcapital_html_processed_reports.json"
TELEGRAM_BOT_TOKEN = os.getenv("JCAPITAL_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("JCAPITAL_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)

# User agents list
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
]


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


def get_random_headers():
    """Generate random headers for requests"""
    return {
        "User-Agent": USER_AGENTS[int(time.time()) % len(USER_AGENTS)],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": str(uuid.uuid4()),
        "X-Request-Time": str(int(time.time())),
    }


def parse_date(date_str):
    """Parse various date formats from the HTML"""
    if not date_str:
        return None

    date_str = date_str.strip()

    date_patterns = [
        r"(\d{1,2}/\d{1,2}/\d{4})",  # MM/DD/YYYY or M/D/YYYY
        r"(\d{1,2}-\w{3}-\d{2})",  # DD-Mon-YY
        r"(\d{1,2}-\w{3}-\d{4})",  # DD-Mon-YYYY
        r"(\d{1,2}/\d{1,2}/\d{2})",  # MM/DD/YY
    ]

    for pattern in date_patterns:
        match = re.search(pattern, date_str)
        if match:
            try:
                date_part = match.group(1)
                for fmt in ["%m/%d/%Y", "%d-%b-%y", "%d-%b-%Y", "%m/%d/%y"]:
                    try:
                        return datetime.strptime(date_part, fmt)
                    except ValueError:
                        continue
            except Exception:
                continue

    return None


def extract_ticker(text):
    """Extract ticker symbols from text"""
    if not text:
        return None

    ticker_patterns = [
        r"\(([A-Z]{1,5})\)",  # (TICKER)
        r"\(([A-Z]{1,5}\.[A-Z]{1,3})\)",  # (TICKER.EX)
        r"\(([A-Z]{1,5}\s[A-Z]{1,3})\)",  # (TICKER EX)
        r"<strong>\(([A-Z]{1,5})\)</strong>",  # <strong>(TICKER)</strong>
    ]

    for pattern in ticker_patterns:
        matches = re.findall(pattern, text)
        if matches:
            return matches[0].strip()

    return None


def parse_reports_content(html_content):
    """Parse the HTML content to extract report information"""
    soup = BeautifulSoup(html_content, "html.parser")

    # Find the specific div containing the reports
    reports_div = soup.select_one(
        "#wsite-content > div > div > div > div > div > div:nth-child(2)"
    )

    if not reports_div:
        # Fallback to find div with class "paragraph"
        reports_div = soup.find("div", class_="paragraph")

    if not reports_div:
        log_message("Could not find reports content div", "WARNING")
        return []

    reports = []

    # Split content by <br> tags to get individual reports
    content_parts = str(reports_div).split("<br")

    for part in content_parts:
        if not part or len(part.strip()) < 10:
            continue

        # Parse this part as HTML
        part_soup = BeautifulSoup(part, "html.parser")

        links = part_soup.find_all("a", href=True)

        for link in links:
            href = link.get("href", "")
            title = link.get_text(strip=True)

            if not href or not title:
                continue

            if "terms-of-service" not in href and not href.endswith(".pdf"):
                continue

            parent_text = part_soup.get_text()
            ticker = extract_ticker(parent_text)

            date_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", parent_text)
            report_date = None
            if date_match:
                try:
                    report_date = datetime.strptime(date_match.group(1), "%m/%d/%Y")
                except ValueError:
                    pass

            full_url = href
            if href.startswith("/"):
                full_url = "https://www.jcapitalresearch.com" + href

            report_info = {
                "title": title,
                "url": full_url,
                "ticker": ticker,
                "date": report_date.isoformat() if report_date else None,
                "raw_text": parent_text[:200],  # First 200 chars for debugging
            }

            reports.append(report_info)

    log_message(f"Parsed {len(reports)} reports from HTML", "INFO")
    return reports


async def fetch_html(session):
    """Fetch HTML content from the reports page"""
    headers = get_random_headers()

    try:
        async with session.get(
            HTML_URL,
            headers=headers,
            timeout=10,
        ) as response:
            if response.status == 200:
                content = await response.text()
                log_message("Successfully fetched HTML content", "INFO")
                return content
            else:
                log_message(f"Failed to fetch HTML: HTTP {response.status}", "ERROR")
                return None
    except Exception as e:
        log_message(f"Error fetching HTML: {e}", "ERROR")
        return None


async def send_to_telegram(report_data):
    current_time = get_current_time()

    message = f"<b>New J Capital Research Report!</b>\n\n"
    message += f"<b>Title:</b> {report_data['title']}\n"

    if report_data.get("ticker"):
        message += f"<b>Ticker:</b> {report_data['ticker']}\n"

    if report_data.get("date"):
        try:
            report_date = datetime.fromisoformat(report_data["date"])
            report_date_est = report_date.astimezone(pytz.timezone("America/Chicago"))
            message += f"<b>Report Date:</b> {report_date_est.strftime('%Y-%m-%d')}\n"
        except Exception:
            pass

    message += f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>URL:</b> {report_data['url']}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Report sent to Telegram: {report_data['url']}", "INFO")


async def run_scraper():
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
                html_content = await fetch_html(session)

                if not html_content:
                    log_message("Failed to fetch HTML content", "ERROR")
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                reports = parse_reports_content(html_content)

                new_reports = [
                    report
                    for report in reports
                    if report["url"] not in processed_reports
                ]

                if new_reports:
                    log_message(
                        f"Found {len(new_reports)} new reports to process.", "INFO"
                    )

                    for report in new_reports:
                        await send_to_telegram(report)
                        processed_reports.add(report["url"])

                    save_processed_reports(processed_reports)
                else:
                    log_message("No new reports found.", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
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
