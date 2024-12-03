import asyncio
import json
import os
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from time import time
from typing import Dict, List, Set
from uuid import uuid4

import pytz
import requests
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("CNBC_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CNBC_SCRAPER_TELEGRAM_GRP")
LATEST_ASSETS_SHA = os.getenv("CNBC_SCRAPER_LATEST_ASSETS_SHA")
DATA_DIR = Path("data")
ALERTS_FILE = DATA_DIR / "cnbc_html_alerts.json"

# Global variables to store previous alerts
previous_trade_alerts = set()


def load_saved_alerts() -> Set[str]:
    """Load previously saved alerts from disk"""
    try:
        # Create data directory if it doesn't exist
        DATA_DIR.mkdir(exist_ok=True)

        if ALERTS_FILE.exists():
            with open(ALERTS_FILE, "r") as f:
                data = json.load(f)
                trade_alerts = set(data.get("trade_alerts", []))
                articles = set(data.get("articles", []))
                log_message(
                    f"Loaded {len(trade_alerts)} trade alerts and {len(articles)} articles from disk"
                )
                return trade_alerts
        return set()
    except Exception as e:
        log_message(f"Error loading saved alerts: {e}", "ERROR")
        return set()


def save_alerts(trade_alerts: Set[str]):
    """Save alerts to disk"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        data = {"trade_alerts": list(trade_alerts)}
        with open(ALERTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log_message(f"Error saving alerts: {e}", "ERROR")


async def fetch_latest_assets() -> List[Dict]:
    """Fetch latest alerts from CNBC Investing Club"""
    try:
        base_url = "https://webql-redesign.cnbcfm.com/graphql"
        timestamp = int(time() * 10000)
        cache_uuid = uuid4()

        variables = {
            "id": "15838187",
            "offset": 0,
            "pageSize": 1,
            "nonFilter": True,
            "includeNative": False,
            "include": [],
        }

        extensions = {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": LATEST_ASSETS_SHA,
            }
        }
        params = {
            "operationName": "getAssetList",
            "variables": json.dumps(variables),
            "extensions": json.dumps(extensions),
            "cache-timestamp": str(timestamp),
            "cache-uuid": str(cache_uuid),
        }

        headers = {
            "cache-control": "no-cache, no-store, max-age=0, must-revalidate, private",
            "pragma": "no-cache",
            "priority": "u=0, i",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        # Create encoded URL, timestamp and uuid for caching bypass
        encoded_url = f"{base_url}?{urllib.parse.urlencode(params)}"

        response = requests.get(encoded_url, headers=headers)
        response.raise_for_status()

        response_json = response.json()

        # Process content
        assets = response_json.get("data", {}).get("assetList", {}).get("assets", [])

        return assets
    except Exception as e:
        log_message(f"Error fetching alerts: {e}", "ERROR")
        return []


async def check_for_new_alerts():
    """Check for new alerts and send to Telegram if found"""
    global previous_trade_alerts

    try:
        start = time()
        current_alerts = await fetch_latest_assets()
        log_message(f"fetch_latest_assets took {(time() - start):.2f} seconds")

        alerts_updated = False

        # Process each alert
        for alert in current_alerts:
            alert_id = alert["id"]
            alert_type = alert["type"]

            if alert_id not in previous_trade_alerts and alert_type == "cnbcnewsstory":
                previous_trade_alerts.add(alert_id)
                alerts_updated = True

                published_date = datetime.strptime(
                    alert["datePublished"], "%Y-%m-%dT%H:%M:%S%z"
                )
                article_timezone = published_date.tzinfo
                current_time = datetime.now(pytz.utc).astimezone(article_timezone)

                message = (
                    f"<b>New Jim cramer assets Found!</b>\n"
                    f"<b>Title:</b> {alert['title']}\n"
                    f"<b>Link:</b> {alert['url']}\n"
                    f"<b>Published Time:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                    f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                    f"<b>Time difference:</b> {(current_time - published_date).total_seconds():.2f} seconds\n"
                )

                await send_telegram_message(
                    message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                )
                log_message(f"Sent new Alert to Telegram - Title: {alert['title']}")

        # Save alerts if there were any updates
        if alerts_updated:
            save_alerts(previous_trade_alerts)

    except Exception as e:
        log_message(f"Error checking alerts: {e}", "ERROR")


async def main():
    global previous_trade_alerts

    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    # Load saved alerts at startup
    previous_trade_alerts = load_saved_alerts()

    log_message("Starting CNBC alert monitor...")

    while True:
        try:
            await check_for_new_alerts()
            await asyncio.sleep(1)  # Check every second

        except Exception as e:
            log_message(f"Error in main loop: {e}", "ERROR")
            await asyncio.sleep(5)  # Wait longer on error


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...")
        # Save alerts one final time before shutting down
        save_alerts(previous_trade_alerts)
    except Exception as e:
        log_message(f"Critical error: {e}", "CRITICAL")
        sys.exit(1)
