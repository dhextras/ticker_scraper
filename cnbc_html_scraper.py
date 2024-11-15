import asyncio
import json
import os
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from time import time
from typing import Dict, List, Set

import pytz
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("CNBC_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CNBC_SCRAPER_TELEGRAM_GRP")
GMAIL_USERNAME = os.getenv("CNBC_SCRAPER_GMAIL_USERNAME")
GMAIL_PASSWORD = os.getenv("CNBC_SCRAPER_GMAIL_PASSWORD")
LATEST_ARTICLE_SHA = os.getenv("CNBC_SCRAPER_LATEST_ARTICLE_SHA")
SESSION_TOKEN = os.getenv("CNBC_SCRAPER_SESSION_TOKEN")
DATA_DIR = Path("data")
ALERTS_FILE = DATA_DIR / "cnbc_alerts.json"

# Global variables
previous_alerts = set()


def setup_driver():
    """Setup and return a headless Chrome driver"""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--maximize-window")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-search-engine-choice-screen")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])

    service = Service()
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver


def construct_graphql_url():
    """Construct the GraphQL URL with proper parameters"""
    base_url = "https://webql-redesign.cnbcfm.com/graphql"
    variables = {
        "hasICAccess": True,
        "uid": GMAIL_USERNAME,
        "sessionToken": SESSION_TOKEN,
    }
    extensions = {"persistedQuery": {"version": 1, "sha256Hash": LATEST_ARTICLE_SHA}}
    params = {
        "operationName": "notifications",
        "variables": json.dumps(variables),
        "extensions": json.dumps(extensions),
    }
    return f"{base_url}?{urllib.parse.urlencode(params)}"


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


async def fetch_latest_alerts(driver) -> List[Dict]:
    """Fetch latest alerts using Selenium"""
    try:
        url = construct_graphql_url()
        driver.get(url)

        # Wait for the pre element containing the JSON response
        wait = WebDriverWait(driver, 10)
        pre_element = wait.until(EC.presence_of_element_located((By.TAG_NAME, "pre")))

        response_data = json.loads(pre_element.text)

        notifications = response_data.get("data", {}).get("dtcNotifications", {})
        trade_alerts = notifications.get("tradeAlerts", [])
        news = notifications.get("news", [])

        all_alerts = []

        if trade_alerts:
            for alert in trade_alerts:
                all_alerts.append(
                    {
                        "id": alert.get("asset", {}).get("id"),
                        "title": alert.get("asset", {}).get("title"),
                        "url": alert.get("asset", {}).get("url"),
                        "type": "trade_alert",
                        "datePublished": alert.get("asset", {}).get(
                            "dateLastPublished"
                        ),
                    }
                )

        if news:
            for item in news:
                asset = item.get("asset", {})
                if asset and asset.get("section", {}).get("id") == 106983829:
                    all_alerts.append(
                        {
                            "id": asset.get("id"),
                            "title": asset.get("title"),
                            "url": asset.get("url"),
                            "type": "news",
                            "tickerSymbols": asset.get("tickerSymbols"),
                            "datePublished": asset.get("dateLastPublished"),
                        }
                    )

        log_message(f"Found {len(all_alerts)} total alerts")
        return all_alerts
    except Exception as e:
        log_message(f"Error fetching alerts: {e}", "ERROR")
        return []


async def check_for_new_alerts(driver):
    """Check for new alerts and send to Telegram if found"""
    global previous_alerts

    try:
        start = time()
        current_alerts = await fetch_latest_alerts(driver)
        log_message(f"fetch_latest_alerts took {(time() - start):.2f} seconds")

        alerts_updated = False

        for alert in current_alerts:
            alert_id = str(alert["id"])

            if alert_id not in previous_alerts:
                previous_alerts.add(alert_id)
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

                tickerSymbols = alert.get("tickerSymbols", None)
                if tickerSymbols:
                    message += f"<b>Ticker Symbols:</b> {', '.join(tickerSymbols)}\n"

                await send_telegram_message(
                    message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                )
                log_message(f"Sent new {alert['type']} to Telegram")

        if alerts_updated:
            save_alerts(previous_alerts)

    except Exception as e:
        log_message(f"Error checking alerts: {e}", "ERROR")


async def main():
    global previous_alerts

    if not all(
        [
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            GMAIL_USERNAME,
            SESSION_TOKEN,
            LATEST_ARTICLE_SHA,
        ]
    ):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    previous_alerts = load_saved_alerts()
    log_message("Starting CNBC alert monitor...")

    driver = setup_driver()

    try:
        while True:
            try:
                # Wait until market open
                await sleep_until_market_open()
                log_message("Market is open. Starting to check for new alerts...")

                _, _, market_close_time = get_next_market_times()

                while True:
                    current_time = datetime.now(pytz.timezone("America/New_York"))
                    if current_time > market_close_time:
                        log_message("Market is closed. Waiting for next market open...")
                        break

                    start_time = time()
                    await check_for_new_alerts(driver)

                    execution_time = time() - start_time
                    log_message(f"Total iteration time: {execution_time:.2f} seconds")

                    # Adaptive sleep based on execution time
                    await asyncio.sleep(min(1, 1 - execution_time))

            except Exception as e:
                log_message(f"Error in main loop: {e}", "ERROR")
                await asyncio.sleep(5)

    finally:
        # Clean up
        driver.quit()
        save_alerts(previous_alerts)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...")
    except Exception as e:
        log_message(f"Critical error: {e}", "CRITICAL")
        sys.exit(1)
