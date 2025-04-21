import asyncio
import json
import os
from datetime import datetime
from typing import List, NamedTuple

import aiohttp
from dotenv import load_dotenv

from utils.gpt_ticker_extractor import TickerAnalysis, analyze_image_for_ticker
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
CHECK_INTERVAL = 1
TELEGRAM_BOT_TOKEN = os.getenv("BANYAN_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BANYAN_TELEGRAM_GRP")
PROCESSED_JSON_FILE = "data/banyan_processed_images.json"

os.makedirs("data", exist_ok=True)


class ImageSource(NamedTuple):
    name: str
    base_url: str
    image_suffix: str


# Configure image sources
IMAGE_SOURCES = [
    ImageSource(
        name="Microcap Fortunes",
        base_url="https://banyanhill.s3.us-east-1.amazonaws.com/Microcap_Fortunes/Images",
        image_suffix="CMM_SS1.png",
    ),
    ImageSource(
        name="Strategic Fortunes",
        base_url="https://banyanhill.s3.us-east-1.amazonaws.com/StrategicFortunes/Images",
        image_suffix="IKAA_SS1.png",
    ),
    ImageSource(
        name="Extreme Fortunes",
        base_url="https://banyanhill.s3.us-east-1.amazonaws.com/Extreme_Fortunes/images",
        image_suffix="EXF_SS1.png",
    ),
    ImageSource(
        name="8 Figure Fortunes",
        base_url="https://banyanhill.s3.us-east-1.amazonaws.com/8_Figure_Fortunes/images",
        image_suffix="7FF_SS1.png",
    ),
]


def load_processed_urls():
    try:
        with open(PROCESSED_JSON_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_urls(urls):
    with open(PROCESSED_JSON_FILE, "w") as f:
        json.dump(list(urls), f, indent=2)
        log_message("Processed URLs saved.", "INFO")


def generate_image_urls(date: datetime) -> List[str]:
    """Generate image URLs for all sources based on the given date."""
    urls = []

    year = date.strftime("%Y")
    month = date.strftime("%m")
    date_prefix = date.strftime("%m%d%y")

    for source in IMAGE_SOURCES:
        url = f"{source.base_url}/{year}/{month}/{date_prefix}_{source.image_suffix}"
        urls.append((source.name, url))

    return urls


async def check_image_url(session: aiohttp.ClientSession, name: str, url: str) -> bool:
    """Check if an image URL exists and return True if successful."""
    try:
        log_message(f"Checking for available image in url: {url}")

        async with session.get(url) as response:
            if response.status == 200 and "image" in response.headers.get(
                "content-type", ""
            ):
                log_message(f"Found valid image for {name}: {url}", "INFO")
                return True
            return False
    except Exception as e:
        log_message(f"Error checking {name} URL {url}: {e}", "ERROR")
        return False


async def send_to_telegram(name: str, url: str, ticker_obj: TickerAnalysis):
    """Send image URL to Telegram."""
    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Banyan Hill Image Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Source:</b> {name}\n"
    message += f"<b>URL:</b> {url}\n"

    if ticker_obj and ticker_obj.found:
        message += f"\n<b>Ticker:</b> {ticker_obj.ticker}\n"
        message += f"<b>Company:</b> {ticker_obj.company_name}\n"
        message += f"<b>Confidency:</b> {ticker_obj.confidence}\n"

        await send_ws_message(
            {
                "name": f"Banyan - {name}",
                "type": "Buy",
                "ticker": ticker_obj.ticker,
                "sender": "banyan",
                "target": "CSS",
            },
        )
        log_message(
            f"Image sent to Telegram and WebSocket for: {ticker_obj.ticker} - {url}",
            "INFO",
        )
    else:
        log_message(f"Image URL sent to Telegram: {url}", "INFO")

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def run_scraper():
    processed_urls = load_processed_urls()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message("Market is open. Starting to check for new images...", "DEBUG")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking for new image set...")
                image_urls = generate_image_urls(current_time)
                new_image_urls = [
                    (name, url) for name, url in image_urls if url not in processed_urls
                ]

                for name, url in new_image_urls:
                    if await check_image_url(session, name, url):
                        ticker_obj = await analyze_image_for_ticker(url)
                        await send_to_telegram(name, url, ticker_obj)
                        processed_urls.add(url)
                    await asyncio.sleep(CHECK_INTERVAL)

                # Only run the save function when new image available
                if new_image_urls:
                    save_processed_urls(processed_urls)

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    """Main function."""
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        return

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")


if __name__ == "__main__":
    main()
