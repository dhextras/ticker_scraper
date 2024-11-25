import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from time import time
from typing import Dict, List, Set

import pytz
import requests
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("CNBC_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CNBC_SCRAPER_TELEGRAM_GRP")
DATA_DIR = Path("data")
ALERTS_FILE = DATA_DIR / "cnbc_html_alerts.json"

# Global variables to store previous alerts
previous_trade_alerts = set()
previous_articles = set()


def load_saved_alerts() -> tuple[Set[str], Set[str]]:
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
                return trade_alerts, articles
        return set(), set()
    except Exception as e:
        log_message(f"Error loading saved alerts: {e}", "ERROR")
        return set(), set()


def save_alerts(trade_alerts: Set[str], articles: Set[str]):
    """Save alerts to disk"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        data = {"trade_alerts": list(trade_alerts), "articles": list(articles)}
        with open(ALERTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log_message(f"Error saving alerts: {e}", "ERROR")


async def fetch_latest_alerts() -> List[Dict]:
    """Fetch latest alerts from CNBC Investing Club"""
    try:
        url = "https://www.cnbc.com/investingclub/trade-alerts/"
        response = requests.get(url)
        response.raise_for_status()

        # Find the data in the page content
        content = response.text
        data_start = content.find("window.__s_data=")
        data_end = content.find("; window", data_start)

        if data_start == -1 or data_end == -1:
            log_message("Could not find data in page", "ERROR")
            return []

        data_start += len("window.__s_data=")
        data = json.loads(content[data_start:data_end])

        # Extract page date
        articles = []
        page_data = data.get("page", {}).get("page", {})

        # Process featured story hero
        layout = page_data.get("layout", [{}])[0]
        columns = layout.get("columns", [])

        for column in columns:
            modules = column.get("modules", [])
            for module in modules:
                # Check for featured story hero
                if module.get("name") == "featuredStoryHero":
                    featured_assets = module.get("data", {}).get("assets", [])
                    for asset in featured_assets:
                        articles.append(
                            {
                                "id": asset.get("id"),
                                "title": asset.get("title"),
                                "url": asset.get("url"),
                                "type": "trade_alert",
                                "datePublished": asset.get("datePublished"),
                            }
                        )

        log_message(f"Found {len(articles)} total articles")
        return articles
    except Exception as e:
        log_message(f"Error fetching alerts: {e}", "ERROR")
        return []


async def check_for_new_alerts():
    """Check for new alerts and send to Telegram if found"""
    global previous_trade_alerts, previous_articles

    try:
        start = time()
        current_alerts = await fetch_latest_alerts()
        log_message(f"fetch_latest_alerts took {(time() - start):.2f} seconds")

        alerts_updated = False

        # Process each alert
        for alert in current_alerts:
            alert_id = alert["id"]
            alert_set = (
                previous_trade_alerts
                if alert["type"] == "trade_alert"
                else previous_articles
            )

            if alert_id not in alert_set:
                alert_set.add(alert_id)
                alerts_updated = True

                published_date = datetime.strptime(
                    alert["datePublished"], "%Y-%m-%dT%H:%M:%S%z"
                )
                article_timezone = published_date.tzinfo
                current_time = datetime.now(pytz.utc).astimezone(article_timezone)

                message = (
                    f"<b>New {alert['type'].replace('_', ' ').title()} Found!</b>\n"
                    f"<b>Title:</b> {alert['title']}\n"
                    f"<b>Link:</b> {alert['url']}\n"
                    f"<b>Published Time:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                    f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                    f"<b>Time difference:</b> {(current_time - published_date).total_seconds():.2f} seconds\n"
                )

                await send_telegram_message(
                    message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                )
                log_message(f"Sent new {alert['type']} to Telegram")

        # Save alerts if there were any updates
        if alerts_updated:
            save_alerts(previous_trade_alerts, previous_articles)

    except Exception as e:
        log_message(f"Error checking alerts: {e}", "ERROR")


async def main():
    global previous_trade_alerts, previous_articles

    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    # Load saved alerts at startup
    previous_trade_alerts, previous_articles = load_saved_alerts()

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
        save_alerts(previous_trade_alerts, previous_articles)
    except Exception as e:
        log_message(f"Critical error: {e}", "CRITICAL")
        sys.exit(1)
