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
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

# Constants
SITEMAP_URL = "https://www.blueorcacapital.com/wp-sitemap-posts-post-1.xml"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/blueorca_processed_sitemap_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("BLUEORCA_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BLUEORCA_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_processed_urls():
    try:
        with open(PROCESSED_URLS_FILE, "r") as f:
            return {item["url"]: item["lastmod"] for item in json.load(f)}
    except FileNotFoundError:
        return {}


def save_processed_urls(urls_dict):
    with open(PROCESSED_URLS_FILE, "w") as f:
        urls_list = [
            {"url": url, "lastmod": lastmod} for url, lastmod in urls_dict.items()
        ]
        json.dump(urls_list, f, indent=2)
    log_message("Processed sitemap URLs saved.", "INFO")


async def fetch_sitemap(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(SITEMAP_URL, headers=headers) as response:
            if response.status == 200:
                xml = await response.text()
                soup = BeautifulSoup(xml, "xml")

                urls = []
                for url_element in soup.find_all("url"):
                    loc = url_element.find("loc")
                    lastmod = url_element.find("lastmod")

                    if loc and lastmod:
                        urls.append({"url": loc.text, "lastmod": lastmod.text})

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


async def fetch_page_content(session, url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")

                # Try to find the ticker in the content
                ticker_pattern = r"(?:NYSE|NASDAQ|AMEX|ASX|KOSDAQ|HK):\s*([A-Z0-9]+)"
                title = soup.find("title")
                page_text = title.text if title else ""

                # Add content from the first few paragraphs
                paragraphs = soup.find_all("p", limit=5)
                for p in paragraphs:
                    page_text += " " + p.text

                match = re.search(ticker_pattern, page_text)
                ticker = match.group(0) if match else "Ticker not found"

                return ticker
            else:
                log_message(
                    f"Failed to fetch page content: HTTP {response.status}", "ERROR"
                )
                return "Failed to fetch ticker"
    except Exception as e:
        log_message(f"Error fetching page content: {e}", "ERROR")
        return "Error fetching ticker"


async def send_url_to_telegram(url_data, ticker):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    if ticker:
        await send_ws_message(
            {
                "name": "Blue Orca Sitemap",
                "type": "Sell",
                "ticker": ticker,
                "sender": "blueorca",
            },
        )

    message = f"<b>New or Updated Blue Orca Post</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url_data['url']}\n"
    message += f"<b>Last Modified:</b> {url_data['lastmod']}\n"
    message += f"<b>Detected Ticker:</b> {ticker}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"New/updated URL sent to Telegram: {url_data['url']}", "INFO")


async def run_sitemap_monitor():
    processed_urls = load_processed_urls()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message("Market is open. Starting to check sitemap...", "DEBUG")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking sitemap for new or updated URLs...")
                sitemap_urls = await fetch_sitemap(session)

                updates_found = False

                for url_data in sitemap_urls:
                    url = url_data["url"]
                    lastmod = url_data["lastmod"]

                    # Check if URL is new or has been updated
                    if url not in processed_urls or processed_urls[url] != lastmod:
                        log_message(f"Found new/updated URL: {url}", "INFO")
                        ticker = await fetch_page_content(session, url)
                        await send_url_to_telegram(url_data, ticker)
                        processed_urls[url] = lastmod
                        updates_found = True

                if updates_found:
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
