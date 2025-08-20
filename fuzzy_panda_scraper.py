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
REPORT_URL = "https://fuzzypandaresearch.com"
CHECK_INTERVAL = 0.5  # seconds
PROCESSED_REPORTS_FILE = "data/fuzzypanda_processed_reports.json"
TELEGRAM_BOT_TOKEN = os.getenv("FUZZYPANDA_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("FUZZYPANDA_TELEGRAM_GRP")

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


def extract_ticker(text):
    """Extract ticker from text using regex to find capitals in parentheses."""
    if not text:
        return "Unknown"

    # Look for pattern of capital letters in parentheses: (TICKER)
    ticker_match = re.search(r"\(([A-Z]+)\)", text)
    if ticker_match:
        return ticker_match.group(1)
    return "Unknown"


async def fetch_content_and_ticker(session, url):
    """Fetch content from URL and extract ticker."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")

                content_element = soup.select_one("div.entry-content")
                if content_element:
                    content = content_element.text.strip()
                    ticker = extract_ticker(content)
                    if ticker != "Unknown":
                        log_message(
                            f"Extracted ticker '{ticker}' from URL content: {url}",
                            "INFO",
                        )
                        return ticker

                log_message(f"No ticker found in URL content: {url}", "WARNING")
                return "Unknown"
            else:
                log_message(
                    f"Failed to fetch URL content: HTTP {response.status} for {url}",
                    "WARNING",
                )
                return "Unknown"
    except Exception as e:
        log_message(f"Error fetching content from {url}: {e}", "ERROR")
        return "Unknown"


async def fetch_reports(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(REPORT_URL, headers=headers) as response:
            if response.status == 200:
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")

                article_entries = soup.select("#main > div")

                reports = []

                for article in article_entries:
                    date_element = article.select_one("divdate")
                    date = (
                        re.sub(r"\s+", " ", date_element.text).strip()
                        if date_element
                        else "Unknown"
                    )

                    content_element = article.select_one("div.entry-content")
                    content = ""
                    if content_element:
                        content = content_element.text.strip()

                    title_element = article.select_one("h2.entry-title")
                    title = title_element.text.strip() if title_element else "Unknown"

                    url_element = (
                        title_element.select_one("a") if title_element else None
                    )
                    url = (
                        url_element["href"]
                        if url_element and url_element.has_attr("href")
                        else ""
                    )

                    reports.append(
                        {
                            "url": url,
                            "title": title,
                            "content": content,
                            "date": date,
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

    message = f"<b>New Fuzzy Panda Research Report</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>Title:</b> {report['title']}\n"
    message += f"<b>Date:</b> {report['date']}\n"
    message += f"<b>Ticker:</b> {report['ticker']}\n"

    content_summary = (
        report["content"][:300] + "..."
        if len(report["content"]) > 300
        else report["content"]
    )
    message += f"\n<b>Content Summary:</b>\n{content_summary}\n"

    if report["url"]:
        message += f"\n<b>URL:</b> {report['url']}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"New report sent to Telegram: {report['ticker']} - {report['title']}", "INFO"
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
                    url = report["url"]
                    title = report["title"]
                    content = report["content"]

                    ticker = "Unknown"
                    title_ticker = extract_ticker(title)
                    content_ticker = extract_ticker(content)

                    # Priority: title -> content -> URL fetch
                    if title_ticker != "Unknown":
                        ticker = title_ticker
                        log_message(f"Using ticker from title: {ticker}", "WARNING")
                    elif content_ticker != "Unknown":
                        ticker = content_ticker
                        log_message(f"Using ticker from content: {ticker}", "WARNING")
                    elif url:
                        log_message(
                            f"No ticker found in title/content, fetching from URL: {url}",
                            "WARNING",
                        )
                        ticker = await fetch_content_and_ticker(session, url)
                    else:
                        ticker = "Unknown"
                        log_message("No ticker found and no URL available", "WARNING")

                    report["ticker"] = ticker
                    if url not in processed_reports:
                        log_message(
                            f"Found new report: {report['url']}",
                            "INFO",
                        )
                        await send_report_to_telegram(report)
                        processed_reports.add(url)

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
