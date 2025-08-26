import asyncio
import json
import os
import re

import requests
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
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

# Constants
SITEMAP_URL = "https://www.blueorcacapital.com/wp-sitemap-posts-post-1.xml"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/blueorca_processed_sitemap_urls.json"
SESSION_FILE = "data/blueorca_sitemap_session.json"
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
            bypass = bypasser(SITEMAP_URL, SESSION_FILE)

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


def fetch_sitemap(cookies):
    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "Cache-Control": "max-age=0",
        }
        custom_cookies = {"cf_clearance": cookies["cf_clearance"]}

        response = requests.get(SITEMAP_URL, headers=headers, cookies=custom_cookies)

        if response.status_code == 200:
            xml = response.text
            soup = BeautifulSoup(xml, "xml")

            urls = []
            for url_element in soup.find_all("url"):
                loc = url_element.find("loc")
                lastmod = url_element.find("lastmod")

                if loc and lastmod:
                    urls.append({"url": loc.text, "lastmod": lastmod.text})

            log_message(f"Fetched {len(urls)} URLs from sitemap", "INFO")
            return urls, None
        elif response.status_code == 403:
            log_message(
                "Cloudflare clearance expired, refreshing cookies...", "WARNING"
            )
            new_cookies = load_cookies(fresh=True)
            if not new_cookies:
                raise Exception("CF_CLEARANCE Failed: Sitemap")
            return [], new_cookies
        elif 500 <= response.status_code < 600:
            log_message(
                f"Server error {response.status_code}: Temporary issue, safe to ignore if infrequent.",
                "WARNING",
            )
            return [], None
        else:
            log_message(
                f"Failed to fetch sitemap: HTTP {response.status_code}", "ERROR"
            )
            return [], None
    except Exception as e:
        if "CF_CLEARANCE Failed" in str(e):
            raise
        log_message(f"Error fetching sitemap: {e}", "ERROR")
        return [], None


def fetch_page_content(url, cookies):
    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "Cache-Control": "max-age=0",
        }
        custom_cookies = {"cf_clearance": cookies["cf_clearance"]}

        response = requests.get(url, headers=headers, cookies=custom_cookies)

        if response.status_code == 200:
            html = response.text
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
                f"Failed to fetch page content: HTTP {response.status_code}", "ERROR"
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
    cookies = load_cookies()

    if not cookies:
        log_message("Failed to get valid cf_clearance", "CRITICAL")
        return

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
            sitemap_urls, new_cookies = fetch_sitemap(cookies)

            cookies = new_cookies if new_cookies is not None else cookies

            updates_found = False

            for url_data in sitemap_urls:
                url = url_data["url"]
                lastmod = url_data["lastmod"]

                # Check if URL is new or has been updated
                if url not in processed_urls or processed_urls[url] != lastmod:
                    log_message(f"Found new/updated URL: {url}", "INFO")
                    ticker = fetch_page_content(url, cookies)
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
