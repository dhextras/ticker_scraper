import asyncio
import json
import os
import re

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.bypass_cloudflare import bypasser
from utils.gpt_ticker_extractor import TickerAnalysis, analyze_company_name_for_ticker
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
REPORT_URL = "https://www.blueorcacapital.com/category/reports/"
CHECK_INTERVAL = 1  # seconds
PROCESSED_REPORTS_FILE = "data/blueorca_processed_reports.json"
SESSION_FILE = "data/blueorca_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("BLUEORCA_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BLUEORCA_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_cookies(fresh=False):
    try:
        cookies = None
        if not fresh:
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
            bypass = bypasser(REPORT_URL, SESSION_FILE)

            if not bypass or bypass == False:
                return None

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


def fetch_reports(cookies):
    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "Cache-Control": "max-age=0",
        }
        custom_cookies = {"cf_clearance": cookies["cf_clearance"]}

        response = requests.get(REPORT_URL, headers=headers, cookies=custom_cookies)

        if response.status_code == 200:
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            table = soup.select_one("#content > div > div > table")

            if not table:
                log_message("Table not found on the page", "ERROR")
                return None, [], None

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
            return table, reports, None
        elif response.status_code == 403:
            log_message(
                "Cloudflare clearance expired, refreshing cookies...", "WARNING"
            )
            new_cookies = load_cookies(fresh=True)
            if not new_cookies:
                raise Exception("CF_CLEARANCE Failed: Reports")
            return None, [], new_cookies
        elif 500 <= response.status_code < 600:
            log_message(
                f"Server error {response.status_code}: Temporary issue, safe to ignore if infrequent.",
                "WARNING",
            )
            return None, [], None
        else:
            log_message(
                f"Failed to fetch reports: HTTP {response.status_code}", "ERROR"
            )
            return None, [], None
    except Exception as e:
        if "CF_CLEARANCE Failed" in str(e):
            raise
        log_message(f"Error fetching reports: {e}", "ERROR")
        return None, [], None


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

    if ticker:
        await send_ws_message(
            {
                "name": "Blue Orca Report",
                "type": "Sell",
                "ticker": ticker,
                "sender": "blueorca",
            },
        )

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
    cookies = load_cookies()

    if not cookies:
        log_message("Failed to get valid cf_clearance", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()
        await initialize_websocket()
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
            table, reports, new_cookies = fetch_reports(cookies)

            cookies = new_cookies if new_cookies is not None else cookies

            for report in reports:
                report_url = report["url"]
                if report_url not in processed_reports:
                    log_message(f"Found new report: {report['title']}", "INFO")

                    if report["ticker"] == "":
                        ticker_obj: TickerAnalysis = (
                            await analyze_company_name_for_ticker([], report["title"])
                        )

                        if ticker_obj and ticker_obj.found:
                            report["ticker"] = ticker_obj.ticker
                            report["company"] = ticker_obj.company_name

                    await send_report_to_telegram(report)
                    processed_reports.add(report_url)

                    # FIX: Remove this after fixing the issue
                    date = get_current_time().strftime("%Y_%m_%d_%H_%M_%S")
                    with open(f"data/blue_orca_report_remove_{date}.html", "w") as f:
                        f.write(str(table))

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
