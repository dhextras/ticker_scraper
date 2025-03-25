import asyncio
import json
import os
import random
import re
import sys
import uuid
from pathlib import Path
from time import time
from typing import Set, Tuple

import requests
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("INVESTOR_PLACE_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("INVESTOR_PLACE_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
IPA_LOGIN_COOKIE = os.getenv("IPA_LOGIN_COOKIE")
CHECK_INTERVAL = 1
DATA_DIR = Path("data")
ALERTS_FILE = DATA_DIR / "investorplace_alerts_new.json"
BASE_URL = "https://investorplace.com/acceleratedprofits"
OEMBED_BASE_URL = (
    "https://investorplace.com/acceleratedprofits/wp-json/oembed/1.0/embed"
)

previous_alerts = set()


def get_random_cache_buster() -> Tuple[str, str]:
    """
    Generate a random cache-busting parameter.

    Returns:
        Tuple of (parameter_name, parameter_value)
    """
    cache_busters = [
        ("cache_timestamp", lambda: str(int(time() * 10000))),
        ("request_uuid", lambda: str(uuid.uuid4())),
        ("cache_time", lambda: str(int(time()))),
        ("ran_time", lambda: str(int(time() * 1000))),
        ("no_cache_uuid", lambda: str(uuid.uuid4().hex[:16])),
        ("unique", lambda: f"{int(time())}-{random.randint(1000, 9999)}"),
        ("req_uuid", lambda: f"req-{uuid.uuid4().hex[:8]}"),
        ("tist", lambda: str(int(time()))),
    ]
    variable, value_generator = random.choice(cache_busters)
    return variable, value_generator()


def load_saved_alerts() -> Set[str]:
    try:
        DATA_DIR.mkdir(exist_ok=True)
        if ALERTS_FILE.exists():
            with open(ALERTS_FILE, "r") as f:
                data = json.load(f)
                alerts = set(data.get("alerts", []))
                log_message(f"Loaded {len(alerts)} alerts from disk")
                return alerts
        return set()
    except Exception as e:
        log_message(f"Error loading saved alerts: {e}", "ERROR")
        return set()


def save_alerts(alerts: Set[str]):
    try:
        DATA_DIR.mkdir(exist_ok=True)
        data = {"alerts": list(alerts)}
        with open(ALERTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log_message(f"Error saving alerts: {e}", "ERROR")


def extract_tickers(title):
    tickers = []

    # Pattern for direct mentions (e.g., "Sell IMO", "Buy CAVA")
    direct_pattern = r"(Buy|Sell)\s+([A-Z]{2,5})(?:\s|$|,|;)"
    direct_matches = re.finditer(direct_pattern, title)
    for match in direct_matches:
        action, ticker = match.groups()
        tickers.append((action, ticker))

    # Pattern for parenthetical mentions (e.g., "Sell Novo Nordisk A/S (NVO)")
    paren_pattern = r"(Buy|Sell)[^()]*?\(([A-Z]{2,5})\)"
    paren_matches = re.finditer(paren_pattern, title)
    for match in paren_matches:
        action, ticker = match.groups()
        tickers.append((action, ticker))

    # Pattern for take profits in (e.g., "Take Profits in CLS")
    profit_pattern = r"Take Profits in\s+([A-Z]{2,5})"
    profit_matches = re.finditer(profit_pattern, title)
    for match in profit_matches:
        ticker = match.group(1)
        tickers.append(("Buy", ticker))

    return tickers


def generate_urls(current_time):
    date_str = current_time.strftime("%Y%m%d")
    year = current_time.strftime("%Y")
    month = current_time.strftime("%m")
    day = current_time.strftime("%d")

    # Generate potential URLs
    post_urls = [
        f"{BASE_URL}/{year}/{month}/{day}/{date_str}-buy-alert/",
        f"{BASE_URL}/{year}/{month}/{day}/{date_str}-sell-alert/",
        f"{BASE_URL}/{year}/{month}/{day}/{date_str}-alert/",
    ]

    # Generate OEmbed URLs with cache busting
    oembed_urls = []
    for url in post_urls:
        cache_param, cache_value = get_random_cache_buster()
        oembed_url = f"{OEMBED_BASE_URL}?url={url}&{cache_param}={cache_value}"
        oembed_urls.append((url, oembed_url))

    return oembed_urls


def fetch_oembed_content(original_url, oembed_url):
    try:
        headers = {
            "Cookie": f"ipa_login={IPA_LOGIN_COOKIE}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }

        response = requests.get(oembed_url, headers=headers)

        if response.status_code == 404:
            return 404, None

        if response.status_code == 200:
            try:
                oembed_data = response.json()
                title = oembed_data.get("title")

                if title:
                    log_message(f"Found a valid OEmbed article: {original_url}", "INFO")
                    return 200, title
                else:
                    log_message(
                        f"No title found in OEmbed response for {original_url}", "INFO"
                    )
                    return 404, None
            except json.JSONDecodeError:
                log_message(f"Failed to parse OEmbed JSON for {original_url}", "ERROR")
                return 500, None

        elif 500 <= response.status_code < 600:
            log_message(
                f"Server error {response.status_code}: Temporary issue, safe to ignore if infrequent.",
                "WARNING",
            )
            return response.status_code, None

        log_message(
            f"Error fetching OEmbed status code: {response.status_code}", "INFO"
        )
        return response.status_code, None

    except Exception as e:
        log_message(f"Error fetching OEmbed: {e}", "ERROR")
        return None, None


async def process_alert() -> bool:
    global previous_alerts
    current_time = get_current_time()
    oembed_url_pairs = generate_urls(current_time)

    for original_url, oembed_url in oembed_url_pairs:
        if original_url in previous_alerts:
            continue

        status_code, title = fetch_oembed_content(original_url, oembed_url)

        if status_code == 404:
            continue

        if status_code == 200 and title:
            tickers = extract_tickers(title)

            ticker_text = "\n".join(
                [f"- {action}: {ticker}" for action, ticker in tickers]
            )

            message = (
                f"<b>New InvestorPlace Alert (New Method)!</b>\n"
                f"<b>Title:</b> {title}\n"
                f"<b>URL:</b> {original_url}\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            )

            if tickers:
                message += f"\n<b>Detected Tickers:</b>\n{ticker_text}"

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            log_message(f"Sent new alert to Telegram: {original_url}")

            previous_alerts.add(original_url)
            save_alerts(previous_alerts)
            return True

    return False


async def run_scraper():
    global previous_alerts
    previous_alerts = load_saved_alerts()

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new posts...", "DEBUG")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()
            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                break

            log_message("Checking for new alerts...")
            try:
                found_alert = await process_alert()
                if found_alert:
                    sleep_seconds = (
                        market_close_time - get_current_time()
                    ).total_seconds()
                    log_message(
                        f"Valid alert found for today. Waiting for {sleep_seconds:.2f} seconds until market close.",
                        "WARNING",
                    )
                    await asyncio.sleep(sleep_seconds)
            except Exception as e:
                log_message(f"Error in scraper loop: {e}", "ERROR")

            await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, IPA_LOGIN_COOKIE]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        save_alerts(previous_alerts)
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
