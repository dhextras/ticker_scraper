import asyncio
import json
import os
import re
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
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

# Constants
CURRENT_YEAR = datetime.now().year
API_URL = f"https://api.jetboost.io/search?boosterId=cm0y7l4eh05160638kzkaimu4&q={CURRENT_YEAR}"
CHECK_INTERVAL = 0.5  # seconds
PROCESSED_RELEASES_FILE = "data/jetboost_processed_press_releases.json"
PRESS_RELEASE_BASE_URL = "https://www.sprucepointcap.com/press-releases/"
TELEGRAM_BOT_TOKEN = os.getenv("SPRUCEPOINT_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("SPRUCEPOINT_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_processed_releases():
    try:
        with open(PROCESSED_RELEASES_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_releases(releases):
    with open(PROCESSED_RELEASES_FILE, "w") as f:
        json.dump(list(releases), f, indent=2)
    log_message("Processed press releases saved.", "INFO")


async def fetch_releases_from_api(session):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }

        async with session.get(API_URL, headers=headers) as response:
            if response.status == 200:
                data = await response.json()

                release_slugs = [slug for slug, value in data.items() if value is True]

                log_message(
                    f"Fetched {len(release_slugs)} press releases from JetBoost API",
                    "INFO",
                )
                return release_slugs
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return []
            else:
                log_message(
                    f"Failed to fetch press releases from API: HTTP {response.status}",
                    "ERROR",
                )
                return []
    except Exception as e:
        log_message(f"Error fetching press releases from API: {e}", "ERROR")
        return []


def extract_ticker_from_slug(slug):
    """Extract ticker from press release slug."""
    # Pattern to match the ending ticker (last word after the last dash)
    pattern = r"-([a-zA-Z]+)$"
    matches = re.search(pattern, slug)

    if matches:
        return matches.group(1).upper()

    # Fallback: extract from the nasdaq/nyse portion
    exchange_pattern = r"(nasdaq|nyse)-([a-zA-Z]+)"
    exchange_matches = re.search(exchange_pattern, slug, re.IGNORECASE)

    if exchange_matches:
        return exchange_matches.group(2).upper()

    return None


def extract_company_name_from_slug(slug):
    """Extract company name from press release slug."""
    # Remove the prefix and suffix
    prefix = "spruce-point-capital-management-announces-investment-opinion-releases-report-and-strong-sell-research-opinion-on-"
    if slug.startswith(prefix):
        company_part = slug[len(prefix) :]

        # Remove the ticker/exchange suffix
        company_part = re.sub(
            r"-(nasdaq|nyse)-[a-zA-Z]+$", "", company_part, flags=re.IGNORECASE
        )

        # Format to title case with spaces
        company_name = " ".join(word.capitalize() for word in company_part.split("-"))
        return company_name

    return "Unknown Company"


async def send_release_to_telegram(release_slug, ticker=None):
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
    release_url = f"{PRESS_RELEASE_BASE_URL}{release_slug}"

    company_name = extract_company_name_from_slug(release_slug)

    message = f"<b>New press release detected - Press Release API</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>Company:</b> {company_name}\n"

    if ticker:
        message += f"<b>Ticker:</b> {ticker}\n"

    message += f"<b>URL:</b> {release_url}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"New press release detected via API and sent to Telegram: {company_name}{' (' + ticker + ')' if ticker else ''}",
        "INFO",
    )


async def process_new_release(slug):
    """Process a newly detected press release."""
    log_message(f"Processing new press release: {slug}", "INFO")

    ticker = extract_ticker_from_slug(slug)

    # if ticker:
    #     await send_ws_message(
    #         {
    #             "name": "SpruePoint - Press Release API",
    #             "type": "Sell",
    #             "ticker": ticker,
    #             "sender": "sprucepoint",
    #             "target": "CSS",
    #         }
    #     )
    #     log_message(f"WebSocket message sent for ticker: {ticker}", "INFO")

    await send_release_to_telegram(slug, ticker)


async def run_api_monitor():
    processed_releases = load_processed_releases()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message(
                "Market is open. Starting to check API for new press releases...",
                "DEBUG",
            )
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking JetBoost API for new press releases...")
                release_slugs = await fetch_releases_from_api(session)

                for slug in release_slugs:
                    if slug not in processed_releases:
                        await process_new_release(slug)
                        processed_releases.add(slug)

                if release_slugs:
                    save_processed_releases(processed_releases)

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
