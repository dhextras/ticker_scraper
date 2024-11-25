import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from time import time
from typing import Dict, List, Set, Tuple

import pytz
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open

load_dotenv()
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from time import time
from typing import Dict, List, Set, Tuple

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("INVESTOR_PLACE_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("INVESTOR_PLACE_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
IPA_LOGIN_COOKIE = os.getenv("IPA_LOGIN_COOKIE")
CHECK_INTERVAL = 1  # seconds
DATA_DIR = Path("data")
ALERTS_FILE = DATA_DIR / "investorplace_alerts.json"

# Global variables to store previous alerts
previous_alerts = set()


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


def extract_tickers(title: str) -> List[Tuple[str, str]]:
    """
    Extract tickers from the title with their associated action (Buy/Sell)
    Returns list of tuples: (action, ticker)
    """
    tickers = []

    # Pattern for direct mentions (e.g., "Sell IMO", "Buy CAVA")
    direct_pattern = r"(Buy|Sell)\s+([A-Z]{2,6})(?:\s|$|,|;)"
    direct_matches = re.finditer(direct_pattern, title)
    for match in direct_matches:
        action, ticker = match.groups()
        tickers.append((action, ticker))

    # Pattern for parenthetical mentions (e.g., "Sell Novo Nordisk A/S (NVO)")
    paren_pattern = r"(Buy|Sell)[^()]*?\(([A-Z]{2,6})\)"
    paren_matches = re.finditer(paren_pattern, title)
    for match in paren_matches:
        action, ticker = match.groups()
        tickers.append((action, ticker))

    return tickers


async def fetch_article_data(url: str) -> Dict:
    """Fetch and parse article data from InvestorPlace"""
    try:
        headers = {"Cookie": f"ipa_login={IPA_LOGIN_COOKIE}"}

        response = requests.get(url, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        schema_script = soup.select_one("head > script.speedyseo-schema-graph")

        if not schema_script:
            raise ValueError("Could not find article schema data")

        schema_data = json.loads(schema_script.string)

        # Find the Article object in the graph
        article_data = next(
            (
                item
                for item in schema_data.get("@graph", [])
                if item.get("@type") == "Article"
            ),
            None,
        )
        print(article_data)

        if not article_data:
            raise ValueError("Could not find Article data in schema")

        return article_data

    except Exception as e:
        log_message(f"Error fetching article data: {e}", "ERROR")
        return {}


async def process_alert() -> None:
    """Check for new alerts and send to Telegram if found"""
    global previous_alerts

    try:
        today = datetime.now()
        url = f"https://investorplace.com/acceleratedprofits/{today.year}/{today.month:02d}/{today.day:02d}/{today.year}{today.month:02d}{today.day:02d}-alert/"

        start = time()
        article_data = await fetch_article_data(url)
        log_message(f"fetch_article_data took {(time() - start):.2f} seconds")

        if not article_data:
            return

        article_id = article_data.get("@id")
        if not article_id or article_id in previous_alerts:
            return

        previous_alerts.add(article_id)
        save_alerts(previous_alerts)

        title = article_data.get("headline", "")
        published_date = datetime.fromisoformat(
            article_data.get("datePublished", "").replace("Z", "+00:00")
        )
        current_time = datetime.now(pytz.utc)

        tickers = extract_tickers(title)
        if tickers:
            for action, ticker in tickers:
                await send_ws_message(
                    {
                        "name": "Navallier Old",
                        "type": action,
                        "ticker": ticker,
                        "sender": "navallier",
                    },
                    WS_SERVER_URL,
                )

        ticker_text = "\n".join([f"- {action}: {ticker}" for action, ticker in tickers])

        message = (
            f"<b>New InvestorPlace Alert!</b>\n"
            f"<b>Title:</b> {title}\n"
            f"<b>URL:</b> {url}\n"
            f"<b>Published Time:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"<b>Time difference:</b> {(current_time - published_date).total_seconds():.2f} seconds\n"
        )

        if tickers:
            message += f"\n<b>Detected Tickers:</b>\n{ticker_text}"

        await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        log_message("Sent new alert to Telegram")

    except Exception as e:
        log_message(f"Error checking alerts: {e}", "ERROR")


async def run_scraper():
    """Main scraper loop that respects market hours"""
    global previous_alerts
    previous_alerts = load_saved_alerts()

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new posts...")

        # Get market times
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))
            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...")
                break

            log_message("Checking for new alerts...")
            try:
                await process_alert()

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
