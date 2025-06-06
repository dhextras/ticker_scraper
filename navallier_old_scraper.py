import asyncio
import json
import os
import random
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from time import time
from typing import List, Set, Tuple

import pytz
import requests
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
TELEGRAM_BOT_TOKEN = os.getenv("INVESTOR_PLACE_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("INVESTOR_PLACE_TELEGRAM_GRP")
IPA_LOGIN_COOKIE = os.getenv("IPA_LOGIN_COOKIE")
CHECK_INTERVAL = 0.3  # seconds
DATA_DIR = Path("data")
ALERTS_FILE = DATA_DIR / "investorplace_alerts.json"
JSON_URL = "https://investorplace.com/acceleratedprofits/wp-json/wp/v2/posts?author=25699,25547&categories=8&per_page=3"
PROXY_FILE = "cred/proxies.json"

# Global variables to store previous alerts
previous_alerts = set()


def load_proxies():
    try:
        with open(PROXY_FILE, "r") as f:
            data = json.load(f)
            proxies = data.get("investor_place", [])
            if not proxies:
                log_message("No proxies found in config", "CRITICAL")
                sys.exit(1)
            return proxies
    except FileNotFoundError:
        log_message(f"Proxy file not found: {PROXY_FILE}", "CRITICAL")
        sys.exit(1)
    except json.JSONDecodeError:
        log_message(f"Invalid JSON in proxy file: {PROXY_FILE}", "CRITICAL")
        sys.exit(1)
    except Exception as e:
        log_message(f"Error loading proxies: {e}", "CRITICAL")
        sys.exit(1)


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
    """Load previously saved alerts from disk"""
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
    """Save alerts to disk"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        data = {"alerts": list(alerts)}
        with open(ALERTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log_message(f"Error saving alerts: {e}", "ERROR")


def extract_tickers(title):
    """
    Extract tickers from the title with their associated action (Buy/Sell)
    Returns list of tuples: (action, ticker)
    """
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


def fetch_flash_alerts(proxy) -> List:
    """Fetch and parse article data from InvestorPlace"""
    try:
        cache_param, cache_value = get_random_cache_buster()
        headers = {"Cookie": f"ipa_login={IPA_LOGIN_COOKIE}"}
        proxies = (
            {"http": f"http://{proxy}", "https": f"http://{proxy}"} if proxy else None
        )

        response = requests.get(
            f"{JSON_URL}&{cache_param}={cache_value}",
            headers=headers,
            proxies=proxies,
            timeout=3,
        )

        if response.status_code == 200:
            return response.json()
        elif 500 <= response.status_code < 600:
            log_message(
                f"Server error {response.status_code}: Temporary issue, safe to ignore if infrequent.",
                "WARNING",
            )
            return []

        log_message(f"Error fetching alerts: {response.status_code}", "WARNING")
        return []

    except requests.Timeout:
        log_message(f"Took more then 3 sec to fetch with proxy: {proxy}", "WARNING")
        return []
    except Exception as e:
        log_message(f"Error fetching article data with proxy {proxy}: {e}", "ERROR")
        return []


async def process_alert(proxy) -> None:
    """Check for new alerts and send to Telegram if found"""
    global previous_alerts

    try:
        start = time()
        flash_alerts = fetch_flash_alerts(proxy)
        log_message(f"fetch_article_data took {(time() - start):.2f} seconds")

        if not flash_alerts or len(flash_alerts) <= 0:
            return

        new_alerts = [
            alert for alert in flash_alerts if alert["link"] not in previous_alerts
        ]

        for alert in new_alerts:
            title = alert["title"]["rendered"]

            published_date = alert["modified_gmt"]
            published_time = datetime.fromisoformat(published_date).astimezone(
                pytz.timezone("America/Chicago")
            )
            current_time = get_current_time()

            tickers = extract_tickers(title)
            if tickers:
                buy_tickers = [
                    (action, ticker)
                    for action, ticker in tickers
                    if action.lower() == "buy"
                ]
                sell_tickers = [
                    (action, ticker)
                    for action, ticker in tickers
                    if action.lower() == "sell"
                ]
                s_action = None
                s_ticker = None

                if len(buy_tickers) > 0:
                    s_action, s_ticker = buy_tickers[0]
                elif len(sell_tickers):
                    s_action, s_ticker = sell_tickers[0]

                if s_action is not None and s_ticker is not None:
                    await send_ws_message(
                        {
                            "name": "Navallier Old",
                            "type": s_action,
                            "ticker": s_ticker,
                            "sender": "navallier",
                        },
                    )

            ticker_text = "\n".join(
                [f"- {action}: {ticker}" for action, ticker in tickers]
            )

            message = (
                f"<b>New InvestorPlace Alert!</b>\n"
                f"<b>Title:</b> {title}\n"
                f"<b>URL:</b> {alert['link']}\n"
                f"<b>Published Time:</b> {published_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>Time difference:</b> {(current_time - published_time).total_seconds():.2f} seconds\n"
            )

            if tickers:
                message += f"\n<b>Detected Tickers:</b>\n{ticker_text}"

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            log_message(f"Sent new alert to Telegram, title: {title}")

            new_links = [alert["link"] for alert in new_alerts]
            previous_alerts.update(new_links)
            save_alerts(previous_alerts)

    except Exception as e:
        log_message(f"Error checking alerts: {e}", "ERROR")


async def run_scraper():
    """Main scraper loop that respects market hours"""
    global previous_alerts
    previous_alerts = load_saved_alerts()

    proxies = load_proxies()
    log_message(f"Loaded {len(proxies)} proxies")

    while True:
        await sleep_until_market_open()
        await initialize_websocket()

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
            proxy = random.choice(proxies)
            try:
                await process_alert(proxy)
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
