import asyncio
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

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
SITEMAP_URL = "https://www.sprucepointcap.com/sitemap.xml"
CHECK_INTERVAL = 60  # seconds
PROCESSED_URLS_FILE = "data/sitemap_processed_urls.json"
BASE_URL = "https://www.sprucepointcap.com"
RESEARCH_PATH = "/research/"
PRESS_RELEASE_PATH = "/press-releases/"
TELEGRAM_BOT_TOKEN = os.getenv("SPRUCEPOINT_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("SPRUCEPOINT_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_processed_urls():
    try:
        with open(PROCESSED_URLS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_urls(urls):
    with open(PROCESSED_URLS_FILE, "w") as f:
        json.dump(list(urls), f, indent=2)
    log_message("Processed URLs saved.", "INFO")


async def fetch_sitemap(session):
    """Fetch and parse the sitemap XML."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(SITEMAP_URL, headers=headers) as response:
            if response.status == 200:
                xml_content = await response.text()

                root = ET.fromstring(xml_content)

                urls = []
                for url_element in root.findall(
                    ".//{http://www.sitemaps.org/schemas/sitemap/0.9}url"
                ):
                    loc_element = url_element.find(
                        "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
                    )
                    if loc_element is not None:
                        url = loc_element.text.strip()
                        urls.append(url)

                log_message(f"Fetched {len(urls)} URLs from sitemap", "INFO")
                return urls
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return []
            else:
                log_message(f"Failed to fetch sitemap: HTTP {response.status}", "ERROR")
                return []
    except Exception as e:
        log_message(f"Error fetching sitemap: {e}", "ERROR")
        return []


def filter_content_urls(urls):
    """Filter URLs to only include research and press releases."""
    research_urls = []
    press_release_urls = []

    for url in urls:
        parsed_url = urlparse(url)
        path = parsed_url.path

        if path.startswith(RESEARCH_PATH) and len(path) > len(RESEARCH_PATH):
            research_urls.append(url)
        elif path.startswith(PRESS_RELEASE_PATH) and len(path) > len(
            PRESS_RELEASE_PATH
        ):
            press_release_urls.append(url)

    return press_release_urls, research_urls


async def fetch_research_content(session, url):
    """Fetch the HTML content of the research page to extract ticker information."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                content = await response.text()
                return content
            else:
                log_message(
                    f"Failed to fetch research content: HTTP {response.status}", "ERROR"
                )
                return None
    except Exception as e:
        log_message(f"Error fetching research content: {e}", "ERROR")
        return None


def extract_ticker_from_research(html_content):
    """Extract the first stock ticker mentioned in the format (NYSE: ABC) or (NASDAQ: ABC)."""
    if not html_content:
        return None

    pattern = r"\((?:NYSE|NASDAQ|Nasdaq|Nyse|NYSEAMEX|OTC|OTCBB):\s*([A-Z]+)\)"
    matches = re.findall(pattern, html_content, re.IGNORECASE)

    if matches:
        return matches[0].upper()
    return None


def extract_ticker_from_press_release_url(url):
    """Extract ticker from press release URL."""
    parsed_url = urlparse(url)
    path = parsed_url.path

    slug = path.split("/")[-1]

    pattern = r"(?:nyse|nasdaq|nyseamex|otcbb|otc)-([a-zA-Z]+)(?:-|$)"
    matches = re.search(pattern, slug, re.IGNORECASE)

    if matches:
        return matches.group(1).upper()

    return None


def extract_company_name_from_url(url):
    """Extract company name from URL for research"""
    parsed_url = urlparse(url)
    path = parsed_url.path

    slug = path.rstrip("/").split("/")[-1]

    company_name = " ".join(word.capitalize() for word in slug.split("-"))
    return company_name


async def process_press_release(url):
    """Process a press release URL."""
    ticker = extract_ticker_from_press_release_url(url)

    log_message(f"Processing press release: {url}", "INFO")
    return {
        "url": url,
        "ticker": ticker,
        "type": "Press Release",
    }


async def process_research(session, url):
    """Process a research URL."""
    log_message(f"Processing research page: {url}", "INFO")

    start_fetch = time.time()
    html_content = await fetch_research_content(session, url)
    ticker = extract_ticker_from_research(html_content)
    fetch_time = time.time() - start_fetch

    company_name = extract_company_name_from_url(url)

    log_message(
        f"Extracted company: {company_name}, ticker: {ticker}, fetch time: {fetch_time}s",
        "INFO",
    )

    return {
        "url": url,
        "ticker": ticker,
        "company": company_name,
        "type": "Research",
        "fetch_time": fetch_time,
    }


async def send_to_websocket(processed_items):
    """Send all processed items to websocket first."""
    log_message(f"Sending {len(processed_items)} items to WebSocket", "INFO")

    for item in processed_items:
        if item["ticker"]:
            await send_ws_message(
                {
                    "name": f"SpruePoint - {item['type']} XML",
                    "type": "Sell",
                    "ticker": item["ticker"],
                    "sender": "sprucepoint",
                    "target": "CSS",
                }
            )
            log_message(f"WebSocket message sent for ticker: {item['ticker']}", "INFO")


async def send_to_telegram(processed_items):
    """Send all processed items to Telegram after websocket."""
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    for item in processed_items:
        message = f"<b>New {item['type']} detected - Sitemap Monitor</b>\n\n"
        message += f"<b>Current Time:</b> {timestamp}\n"

        if item["type"] == "Research" and "fetch_time" in item:
            message += f"<b>Article Fetch Time:</b> {item['fetch_time']:.2f}s\n"

        if "company" in item:
            message += f"<b>Company:</b> {item['company']}\n"

        if item["ticker"]:
            message += f"<b>Ticker:</b> {item['ticker']}\n"

        message += f"<b>URL:</b> {item['url']}"

        await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
        log_message(
            f"New {item['type']} sent to Telegram: {' (' + item['ticker'] + ')' if item['ticker'] else ''}",
            "INFO",
        )


async def run_sitemap_monitor():
    processed_urls = load_processed_urls()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message(
                "Market is open. Starting to check sitemap for new content...", "DEBUG"
            )
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking sitemap for new content...")
                all_urls = await fetch_sitemap(session)

                press_release_urls, research_urls = filter_content_urls(all_urls)

                new_press_releases = [
                    url for url in press_release_urls if url not in processed_urls
                ]
                new_research = [
                    url for url in research_urls if url not in processed_urls
                ]

                if new_press_releases or new_research:
                    log_message(
                        f"Found {len(new_press_releases)} new press releases and {len(new_research)} new research pages",
                        "INFO",
                    )

                    processed_items = []

                    for url in new_press_releases:
                        item = await process_press_release(url)
                        processed_items.append(item)
                        processed_urls.add(url)

                    for url in new_research:
                        item = await process_research(session, url)
                        processed_items.append(item)
                        processed_urls.add(url)

                    await send_to_websocket(processed_items)

                    await send_to_telegram(processed_items)

                    save_processed_urls(processed_urls)

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        return

    try:
        asyncio.run(run_sitemap_monitor())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")


if __name__ == "__main__":
    main()
