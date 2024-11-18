import asyncio
import json
import os
import pickle
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN = os.getenv("HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN")
HEDGEYE_SCRAPER_TELEGRAM_GRP = os.getenv("HEDGEYE_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

DATA_DIR = Path("data")
RATE_LIMIT_PROXY_FILE = DATA_DIR / "hedgeye_rate_limited_proxy.json"
LAST_ALERT_FILE = DATA_DIR / "hedgeye_last_alert.json"

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
Path("cred").mkdir(exist_ok=True)


class ProxyManager:
    def __init__(self, proxies: list[str]):
        self.proxies = proxies
        self.current_proxy_index = 0
        self.rate_limited: Dict[str, datetime] = self._load_rate_limited()

    def _load_rate_limited(self) -> Dict[str, datetime]:
        if RATE_LIMIT_PROXY_FILE.exists():
            with open(RATE_LIMIT_PROXY_FILE, "r") as f:
                rate_limited = json.load(f)
                return {k: datetime.fromisoformat(v) for k, v in rate_limited.items()}
        return {}

    def _save_rate_limited(self):
        with open(RATE_LIMIT_PROXY_FILE, "w") as f:
            rate_limited = {k: v.isoformat() for k, v in self.rate_limited.items()}
            json.dump(rate_limited, f)

    def get_next_proxy(self) -> Optional[str]:
        current_time = datetime.now()

        # Remove expired rate limits (30 minutes)
        expired_proxies = [
            proxy
            for proxy, limit_time in self.rate_limited.items()
            if (current_time - limit_time).total_seconds() >= 1800
        ]

        for proxy in expired_proxies:
            del self.rate_limited[proxy]
            log_message(
                f"Proxy {proxy} removed from rate limits (30-minute expired)", "INFO"
            )

        if expired_proxies:
            self._save_rate_limited()

        available_proxies = [
            proxy for proxy in self.proxies if proxy not in self.rate_limited
        ]

        if not available_proxies:
            return None

        proxy = available_proxies[self.current_proxy_index % len(available_proxies)]
        self.current_proxy_index += 1
        return proxy

    def mark_rate_limited(self, proxy: str):
        self.rate_limited[proxy] = datetime.now()
        self._save_rate_limited()
        log_message(f"Marked proxy {proxy} as rate limited", "WARNING")


def setup_driver(proxy: Optional[str] = None) -> webdriver.Chrome:
    """Setup and return a Chrome driver with proxy if provided"""
    chrome_options = Options()
    # chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--maximize-window")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-search-engine-choice-screen")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])

    if proxy:
        chrome_options.add_argument(f"--proxy-server=http://{proxy}")

    service = Service()
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.set_page_load_timeout(60)
    return driver


def load_credentials():
    with open("cred/hedgeye_credentials.json", "r") as f:
        data = json.load(f)
        # Only take the first account since we're not rotating
        account = data["accounts"][0]
        return account["email"], account["password"], data["proxies"]


def save_session(driver, filename):
    with open(filename, "wb") as f:
        pickle.dump(driver.get_cookies(), f)


def load_session(driver, filename):
    try:
        with open(filename, "rb") as f:
            cookies = pickle.load(f)
            driver.get("https://app.hedgeye.com")
            for cookie in cookies:
                driver.add_cookie(cookie)
            return True
    except Exception as e:
        log_message(f"Error loading session: {str(e)}", "ERROR")
        return False


def login(driver, email: str, password: str) -> bool:
    try:
        driver.get("https://accounts.hedgeye.com/users/sign_in")

        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.ID, "user_email"))
        )

        email_input = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "user_email"))
        )
        email_input.send_keys(email)

        password_input = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "user_password"))
        )
        password_input.send_keys(password)
        password_input.send_keys(Keys.RETURN)

        WebDriverWait(driver, 60).until(
            EC.url_changes("https://accounts.hedgeye.com/users/sign_in")
        )

        return True
    except Exception as e:
        log_message(f"Login failed: {str(e)}", "ERROR")
        return False


def load_last_alert():
    if LAST_ALERT_FILE.exists():
        with open(LAST_ALERT_FILE, "r") as f:
            return json.load(f)
    return {}


def fetch_alert_details(driver):
    try:
        driver.get("https://app.hedgeye.com/feed_items/all")

        # Check for rate limiting
        if "403" in driver.current_url or "429" in driver.current_url:
            raise Exception("Rate limited")

        # Wait for content to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "article__header"))
        )

        soup = BeautifulSoup(driver.page_source, "html.parser")

        alert_title = soup.select_one(".article__header")
        if not alert_title:
            return None
        alert_title = alert_title.get_text(strip=True)

        alert_price = soup.select_one(".currency.se-live-or-close-price")
        if not alert_price:
            return None
        alert_price = alert_price.get_text(strip=True)

        current_time_edt = datetime.now(pytz.utc).astimezone(
            pytz.timezone("America/New_York")
        )

        created_at_utc = soup.select_one("time[datetime]")["datetime"]
        created_at = datetime.fromisoformat(created_at_utc.replace("Z", "+00:00"))
        created_at_edt = created_at.astimezone(pytz.timezone("America/New_York"))

        return {
            "title": alert_title,
            "price": alert_price,
            "created_at": created_at_edt,
            "current_time": current_time_edt,
        }

    except Exception as e:
        if "Rate limited" in str(e):
            raise
        log_message(f"Error fetching alert details: {str(e)}", "ERROR")
        return None


async def monitor_feeds():
    email, password, proxies = load_credentials()
    proxy_manager = ProxyManager(proxies)
    session_filename = "data/hedgeye_session.pkl"
    market_is_open = False
    current_driver = None
    current_proxy = None

    try:
        while True:
            pre_market_login_time, market_open_time, market_close_time = (
                get_next_market_times()
            )
            current_time_edt = datetime.now(pytz.timezone("America/New_York"))

            if pre_market_login_time <= current_time_edt < market_open_time:
                if current_driver is None:
                    log_message("Setting up new driver and logging in...", "INFO")
                    current_proxy = proxy_manager.get_next_proxy()
                    if not current_proxy:
                        log_message("No available proxies", "ERROR")
                        await asyncio.sleep(60)
                        continue

                    current_driver = setup_driver(current_proxy)

                    # Try to load saved session first
                    if not (
                        os.path.exists(session_filename)
                        and load_session(current_driver, session_filename)
                    ):
                        if login(current_driver, email, password):
                            save_session(current_driver, session_filename)
                            log_message("Login successful", "INFO")
                        else:
                            current_driver.quit()
                            current_driver = None
                            await asyncio.sleep(60)
                            continue

            elif market_open_time <= current_time_edt <= market_close_time:
                if not market_is_open:
                    log_message("Market is open, starting monitoring...", "INFO")
                    market_is_open = True

                try:
                    if current_driver is None:
                        current_proxy = proxy_manager.get_next_proxy()
                        if not current_proxy:
                            log_message("No available proxies", "ERROR")
                            await asyncio.sleep(60)
                            continue

                        current_driver = setup_driver(current_proxy)
                        if not load_session(current_driver, session_filename):
                            if not login(current_driver, email, password):
                                current_driver.quit()
                                current_driver = None
                                await asyncio.sleep(60)
                                continue
                            save_session(current_driver, session_filename)

                    alert_details = fetch_alert_details(current_driver)

                    if alert_details:
                        last_alert = load_last_alert()
                        if not last_alert or alert_details["title"] != last_alert.get(
                            "title"
                        ):

                            signal_type = (
                                "Buy"
                                if "buy" in alert_details["title"].lower()
                                else (
                                    "Sell"
                                    if "sell" in alert_details["title"].lower()
                                    else "None"
                                )
                            )
                            ticker_match = re.search(
                                r"\b([A-Z]{1,5})\b(?=\s*\$)", alert_details["title"]
                            )
                            ticker = ticker_match.group(0) if ticker_match else "-"

                            await send_ws_message(
                                {
                                    "name": "Hedgeye",
                                    "type": signal_type,
                                    "ticker": ticker,
                                    "sender": "hedgeye",
                                },
                                WS_SERVER_URL,
                            )

                            message = (
                                f"HTML Implementation\n\n"
                                f"Title: {alert_details['title']}\n"
                                f"Price: {alert_details['price']}\n"
                                f"Created At: {alert_details['created_at'].strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                                f"Current Time: {alert_details['current_time'].strftime('%Y-%m-%d %H:%M:%S %Z')}"
                            )

                            await send_telegram_message(
                                message,
                                HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN,
                                HEDGEYE_SCRAPER_TELEGRAM_GRP,
                            )

                            with open(LAST_ALERT_FILE, "w") as f:
                                json.dump(
                                    {
                                        "title": alert_details["title"],
                                        "price": alert_details["price"],
                                        "created_at": alert_details[
                                            "created_at"
                                        ].isoformat(),
                                    },
                                    f,
                                )

                except Exception as e:
                    if "Rate limited" in str(e) and current_proxy:
                        proxy_manager.mark_rate_limited(current_proxy)
                        if current_driver:
                            current_driver.quit()
                        current_driver = None
                    else:
                        log_message(f"Error during monitoring: {str(e)}", "ERROR")

                await asyncio.sleep(1)

            else:
                market_is_open = False
                if current_driver:
                    current_driver.quit()
                    current_driver = None
                await sleep_until_market_open()

    except Exception as e:
        log_message(f"Critical error in monitor_feeds: {e}", "CRITICAL")
    finally:
        if current_driver:
            current_driver.quit()


def main():
    if not all(
        [
            HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN,
            HEDGEYE_SCRAPER_TELEGRAM_GRP,
            WS_SERVER_URL,
        ]
    ):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(monitor_feeds())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
