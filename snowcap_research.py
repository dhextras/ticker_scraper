import asyncio
import hashlib
import json
import os
import re
import sys

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
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

URL = "https://www.snowcapresearch.com/research"
CHECK_INTERVAL = 1
PROCESSED_ITEMS_FILE = "data/snowcap_processed_items.json"
TELEGRAM_BOT_TOKEN = os.getenv("SNOWCAP_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("SNOWCAP_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_processed_items():
    try:
        with open(PROCESSED_ITEMS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_items(items):
    with open(PROCESSED_ITEMS_FILE, "w") as f:
        json.dump(list(items), f, indent=2)
    log_message("Processed items saved.", "INFO")


def extract_ticker_from_section(soup_section):
    try:
        ticker_elements = soup_section.find_all(
            "div", {"data-testid": "richTextElement"}
        )

        for element in ticker_elements:
            text = element.get_text().strip()
            if ":" in text and any(
                exchange in text for exchange in ["NASDAQ", "NYSE", "LSE"]
            ):
                tickers = re.findall(r"([A-Z]+):([A-Z0-9]+)", text)
                if tickers:
                    return tickers[0][1], tickers[0][0]
        return None, None
    except Exception as e:
        log_message(f"Error extracting ticker: {e}", "ERROR")
        return None, None


def extract_position_type(soup_section):
    try:
        position_elements = soup_section.find_all(
            "div", {"data-testid": "richTextElement"}
        )

        for element in position_elements:
            text = element.get_text().strip().upper()
            if "SHORT" in text:
                return "Sell"
            elif "LONG" in text:
                return "Buy"
        return "Sell"
    except Exception as e:
        log_message(f"Error extracting position type: {e}", "ERROR")
        return "Sell"


def extract_company_name(soup_section):
    try:
        title_element = soup_section.find("div", {"data-testid": "richTextElement"})
        if title_element:
            title_p = title_element.find("p", class_="font_4")
            if title_p:
                return title_p.get_text().strip()
        return None
    except Exception as e:
        log_message(f"Error extracting company name: {e}", "ERROR")
        return None


def extract_presentation_date(soup_section):
    try:
        button = soup_section.find("a", {"data-testid": "linkElement"})
        if button:
            aria_label = button.get("aria-label", "")
            date_match = re.search(r"Presentation - (.+)", aria_label)
            if date_match:
                return date_match.group(1)
        return None
    except Exception as e:
        log_message(f"Error extracting presentation date: {e}", "ERROR")
        return None


async def fetch_page(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(URL, headers=headers) as response:
            if response.status == 200:
                html = await response.text()
                log_message("Fetched page successfully", "INFO")
                return html
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue", "WARNING"
                )
                return None
            else:
                log_message(f"Failed to fetch page: HTTP {response.status}", "ERROR")
                return None
    except Exception as e:
        log_message(f"Error fetching page: {e}", "ERROR")
        return None


def parse_research_sections(html):
    soup = BeautifulSoup(html, "html.parser")
    sections = soup.select(
        'section[data-block-level-container="ClassicSection"] > div > div[data-testid="mesh-container-content"]'
    )

    if len(sections) <= 1:
        return []

    research_items = []
    for section in sections[1:]:
        company_name = extract_company_name(section)
        ticker, exchange = extract_ticker_from_section(section)
        presentation_date = extract_presentation_date(section)
        position_type = extract_position_type(section)

        if company_name and ticker:
            item_hash = hashlib.md5(
                f"{company_name}{ticker}{presentation_date}".encode()
            ).hexdigest()

            research_items.append(
                {
                    "company_name": company_name,
                    "ticker": ticker,
                    "exchange": exchange,
                    "presentation_date": presentation_date,
                    "position_type": position_type,
                    "hash": item_hash,
                }
            )

    return research_items


async def send_items_to_telegram(items):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Snowcap Research items found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Items:</b>\n"

    for item in items:
        message += f"  • {item['company_name']} ({item['exchange']}:{item['ticker']}) - {item['position_type']}\n"
        if item["presentation_date"]:
            message += f"    Date: {item['presentation_date']}\n"
        message += "\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New items sent to Telegram: {len(items)} items", "INFO")


async def send_to_telegram(item):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Snowcap Research Ticker found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Company:</b> {item['company_name']}\n"
    message += f"<b>Ticker:</b> {item['ticker']}\n"
    message += f"<b>Exchange:</b> {item['exchange']}\n"
    message += f"<b>Position:</b> {item['position_type']}\n"
    if item["presentation_date"]:
        message += f"<b>Presentation Date:</b> {item['presentation_date']}\n"

    await send_ws_message(
        {
            "name": "Snowcap Research",
            "type": item["position_type"],
            "ticker": item["ticker"],
            "sender": "snowcap",
        }
    )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"Report sent to Telegram: {item['ticker']} ({item['exchange']})", "INFO"
    )


async def run_scraper():
    processed_items = load_processed_items()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message("Market is open. Starting to check for new items...", "DEBUG")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking for new research items...")
                html = await fetch_page(session)

                if not html:
                    await asyncio.sleep(CHECK_INTERVAL)
                    continue

                research_items = parse_research_sections(html)
                new_items = [
                    item
                    for item in research_items
                    if item["hash"] not in processed_items
                ]

                if new_items:
                    log_message(f"Found {len(new_items)} new items to process.", "INFO")

                    for item in new_items:
                        await send_to_telegram(item)
                        processed_items.add(item["hash"])

                    await send_items_to_telegram(new_items)
                    save_processed_items(processed_items)
                else:
                    log_message("No new items found.", "INFO")

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
