import asyncio
import json
import os
import random
import re
import sys
import time
import urllib.parse
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Dict, List, Set
from uuid import uuid4

import aiohttp
import requests
import undetected_chromedriver as uc
from dotenv import load_dotenv
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

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
TELEGRAM_BOT_TOKEN = os.getenv("CNBC_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CNBC_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
GMAIL_USERNAME = os.getenv("CNBC_SCRAPER_GMAIL_USERNAME")
GMAIL_PASSWORD = os.getenv("CNBC_SCRAPER_GMAIL_PASSWORD")
LATEST_ASSETS_SHA = os.getenv("CNBC_SCRAPER_LATEST_ASSETS_SHA")
ARTICLE_DATA_SHA = os.getenv("CNBC_SCRAPER_ARTICLE_DATA_SHA")

DATA_DIR = Path("data")
ALERTS_FILE = DATA_DIR / "cnbc_alerts.json"
SESSION_TOKEN = os.getenv("CNBC_SCRAPER_SESSION_TOKEN")

# Global variables
last_request_time = 0

# Set up Chrome options
options = uc.ChromeOptions()
options.add_argument("--maximize-window")
options.add_argument("--disable-search-engine-choice-screen")
options.add_argument("--blink-settings=imagesEnabled=false")


class RateLimiter:
    def __init__(self, calls_per_second=2):
        self.calls_per_second = calls_per_second
        self.last_call_time = 0
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            current_time = time.time()
            time_since_last_call = current_time - self.last_call_time
            if time_since_last_call < (1 / self.calls_per_second):
                await asyncio.sleep((1 / self.calls_per_second) - time_since_last_call)
            self.last_call_time = time.time()


rate_limiter = RateLimiter()

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


def timing_decorator(func):
    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        start_time = time.time()
        result = await func(*args, **kwargs)
        end_time = time.time()
        elapsed_time = end_time - start_time
        if elapsed_time > 1:
            log_message(
                f"{func.__name__} took {elapsed_time:.2f} seconds to execute", "ERROR"
            )
        return result

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        elapsed_time = end_time - start_time
        if elapsed_time > 1:
            log_message(
                f"{func.__name__} took {elapsed_time:.2f} seconds to execute", "ERROR"
            )
        return result

    return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper


async def capture_login_response(message):
    global SESSION_TOKEN
    try:
        # Check if the URL matches the login endpoint
        response_url = message.get("params", {}).get("response", {}).get("url", "")

        if "https://register.cnbc.com/auth/api/v3/signin" not in response_url:
            return

        request_id = message.get("params", {}).get("requestId")
        if not request_id:
            return

        # Wait for response to be fully processed
        await asyncio.sleep(2)

        try:
            # Get response body using CDP command
            response_body = driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": request_id}
            )
            response_data = response_body.get("body", "")

            # Parse JSON response
            try:
                response_json = json.loads(response_data)
            except json.JSONDecodeError:
                log_message("Failed to parse response JSON", "WARNING")
                response_json = {}

            # Extract and update session token
            session_token = response_json.get("session_token", SESSION_TOKEN)
            log_message(f"Intercepted Session Token: {session_token}", "INFO")
            SESSION_TOKEN = session_token

        except Exception as e:
            if "No resource with given identifier found" in str(e):
                log_message(
                    "Resource not found or cleared, unable to fetch the response body.",
                    "WARNING",
                )
            else:
                raise e

    except Exception as e:
        log_message(f"Error in capture_login_response: {e}", "ERROR")


async def get_article_data(article_id, uid, session_token):
    await rate_limiter.acquire()
    base_url = "https://webql-redesign.cnbcfm.com/graphql"
    variables = {
        "id": article_id,
        "uid": uid,
        "sessionToken": session_token,
        "pid": 33,
        "bedrockV3API": True,
        "sponsoredProExperienceID": "",
    }
    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": ARTICLE_DATA_SHA,
        }
    }
    params = {
        "operationName": "getArticleData",
        "variables": json.dumps(variables),
        "extensions": json.dumps(extensions),
    }

    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    encoded_url = f"{base_url}?{urllib.parse.urlencode(params)}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(encoded_url, headers=headers) as response:
                if response.status == 200:
                    response_json = await response.json()

                    # Check authentication
                    is_authenticated = (
                        response_json.get("data", {})
                        .get("article", {})
                        .get("body", {})
                        .get("isAuthenticated", False)
                    )

                    if not is_authenticated:
                        log_message(
                            "Authentication required. Please provide a valid session token.",
                            "WARNING",
                        )
                        return None

                    # Process article body
                    article_body = (
                        response_json.get("data", {})
                        .get("article", {})
                        .get("body", {})
                        .get("content", [])
                    )

                    if article_body:
                        for content_block in article_body:
                            if content_block.get("tagName") == "div":
                                for child in content_block.get("children", []):
                                    if child.get("tagName") == "blockquote":
                                        elements = child.get("children", [])

                                        joined_text = ""

                                        def extract_text(element):
                                            # Recursively extract text from allowed tags
                                            text = ""
                                            if isinstance(element, str):
                                                return element
                                            if isinstance(element, dict):
                                                tag = element.get("tagName")
                                                if tag in [
                                                    "p",
                                                    "ul",
                                                    "ol",
                                                    "li",
                                                    "div",
                                                ]:  # Allowed tags
                                                    for child in element.get(
                                                        "children", []
                                                    ):
                                                        text += extract_text(child)
                                            return text

                                        for element in elements:
                                            joined_text += f" {extract_text(element)}"

                                        return joined_text
                    return None
                elif 500 <= response.status < 600:
                    log_message(
                        f"Server error {response.status}: Temporary issue, safe to ignore if infrequent."
                        "WARNING",
                    )
                    return None
                else:
                    log_message(
                        f"Error fetching article data: {response.status}", "ERROR"
                    )
                    return None
        except Exception as e:
            log_message(f"Exception in get_article_data: {e}", "ERROR")
            return None


def get_ticker(data):
    match = re.search(r"shares of\s+([A-Z]+),\s+(\w+)\s+its", data)
    if match:
        ticker = match.group(1)
        action_word = match.group(2).lower()
        action = "Buy" if action_word == "increasing" else "Sell"
        return ticker, action
    return None, None


def get_random_cache_buster():
    """Generate random cache busting url variable for requests"""
    cache_busters = [
        ("timestamp_uniq", lambda: int(time.time() * 10000)),
        ("request_uuid", lambda: str(uuid.uuid4())),
        ("cache_time", lambda: int(time.time())),
        ("ran_time", lambda: int(time.time() * 1000)),
        ("no_cache_uuid", lambda: str(uuid.uuid4().hex[:16])),
        ("unique", lambda: f"{int(time.time())}-{random.randint(1000, 9999)}"),
        ("req_uuid", lambda: f"req-{uuid.uuid4().hex[:8]}"),
        ("tist", lambda: str(int(time.time()))),
    ]

    variable, value_generator = random.choice(cache_busters)
    return (variable, value_generator())


async def fetch_latest_assets() -> List[Dict]:
    """Fetch latest alerts from CNBC Investing Club"""
    try:
        base_url = "https://webql-redesign.cnbcfm.com/graphql"
        timestamp = int(time.time() * 10000)
        cache_uuid = uuid4()
        cache_buster = get_random_cache_buster()

        variables = {
            "id": "15838187",
            "offset": 0,
            "pageSize": 3,
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
            "buster-timestamp": str(timestamp),
            "cache-uuid-buster": str(cache_uuid),
            cache_buster[0]: str(cache_buster[1]),
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


async def process_article(article, uid, session_token, fetch_time):
    try:
        start_time = time.time()
        article_data = await get_article_data(article.get("id"), uid, session_token)
        fetch_data_time = time.time() - start_time

        if article_data:
            published_date = datetime.strptime(
                article["datePublished"], "%Y-%m-%dT%H:%M:%S%z"
            )
            article_timezone = published_date.tzinfo
            ticker, action = get_ticker(article_data)

            if ticker:
                await send_ws_message(
                    {
                        "name": "CNBC",
                        "type": action,
                        "ticker": ticker,
                        "sender": "cnbc",
                    },
                    WS_SERVER_URL,
                )

            current_time = get_current_time().astimezone(article_timezone)
            log_message(
                f"Time difference: {(current_time - published_date).total_seconds():.2f} seconds",
                "INFO",
            )
            message = (
                f"<b>New Article Alert!</b>\n"
                f"<b>Published Date:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                f"<b>Time difference:</b> {(current_time - published_date).total_seconds():.2f} seconds\n"
                f"<b>Assets, Article Data fetch time:</b> {fetch_time:.2f}s, {fetch_data_time:.2f}s\n"
                f"<b>ID:</b> {article['id']}\n"
                f"<b>Title:</b> {article['title']}\n"
                f"<b>Content:</b> {article_data}\n"
            )

            if ticker:
                message += f"\n<b>Ticker:</b> {action} - {ticker}\n"

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            return True
    except Exception as e:
        log_message(f"Error processing article {article.get('id')}: {e}", "ERROR")
    return False


async def check_for_new_alerts(uid, session_token):
    global previous_trade_alerts

    try:
        start = time.time()
        current_articles = await fetch_latest_assets()
        fetch_time = time.time() - start
        log_message(f"fetch_latest_assets took {fetch_time:.2f} seconds")

        articles_updated = False

        # Process each article
        for article in current_articles:
            article_id = article["id"]
            article_type = article["type"]

            if (
                article_id not in previous_trade_alerts
                and article_type == "cnbcnewsstory"
            ):
                previous_trade_alerts.add(article_id)
                articles_updated = True

                await process_article(article, uid, session_token, fetch_time)

        # Save alerts if there were any updates
        if articles_updated:
            save_alerts(previous_trade_alerts)

    except Exception as e:
        log_message(f"Error in check_for_new_alerts: {e}", "ERROR")


async def run_alert_monitor(uid, session_token):
    global previous_trade_alerts

    while True:
        try:
            # Wait until market open
            await sleep_until_market_open()
            log_message(
                "Market is open. Starting to check for new blog posts...", "DEBUG"
            )

            # Get market close time
            _, _, market_close_time = get_next_market_times()

            # Load saved alerts at startup
            previous_trade_alerts = load_saved_alerts()

            # Main market hours loop
            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                try:
                    await check_for_new_alerts(uid, session_token)
                    await asyncio.sleep(0.2)

                except Exception as e:
                    log_message(f"Error checking alerts: {e}", "ERROR")
                    await asyncio.sleep(5)

        except Exception as e:
            log_message(f"Error in monitor loop: {e}", "ERROR")
            await asyncio.sleep(5)


def get_new_session_token():
    global SESSION_TOKEN
    global driver

    try:
        driver = uc.Chrome(enable_cdp_events=True, options=options)
        driver.add_cdp_listener("Network.requestWillBeSent", lambda _: None)
        driver.add_cdp_listener("Network.responseReceived", capture_login_response)

        driver.get("https://www.cnbc.com/investingclub/trade-alerts/")
        time.sleep(random.uniform(2, 5))

        scroll_pause_time = random.uniform(1, 3)
        for _ in range(3):
            driver.execute_script(f"window.scrollBy(0, {random.uniform(300, 500)});")
            time.sleep(scroll_pause_time)

        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(scroll_pause_time)

        action = ActionChains(driver)
        sign_in_button = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.CLASS_NAME, "SignInMenu-signInMenu"))
        )
        action.move_to_element(sign_in_button).perform()
        time.sleep(random.uniform(1, 2))
        sign_in_button.click()

        email_input = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.NAME, "email"))
        )

        if GMAIL_USERNAME is None or GMAIL_PASSWORD is None:
            log_message(f"GMAIL_USERNAME isn't availble in the env", "CRITICAL")
            sys.exit(1)

        email_input.send_keys(GMAIL_USERNAME)
        time.sleep(2)
        password_input = driver.find_element(By.NAME, "password")
        password_input.send_keys(GMAIL_PASSWORD)
        time.sleep(5)

        password_input.send_keys(Keys.ENTER)
        time.sleep(10)

        driver.get("https://www.cnbc.com/investingclub/trade-alerts/")

    except Exception as e:
        log_message(f"Failed to get a new session token: {e}", "ERROR")
        log_message(f"Using existing session token: {SESSION_TOKEN}", "INFO")
    finally:
        driver.quit()


def main():
    uid = GMAIL_USERNAME

    if not all(
        [
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            WS_SERVER_URL,
            SESSION_TOKEN,
            GMAIL_USERNAME,
            GMAIL_PASSWORD,
            LATEST_ASSETS_SHA,
            ARTICLE_DATA_SHA,
        ]
    ):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        # Get initial session token if needed
        if not SESSION_TOKEN:
            get_new_session_token()

        # Start the async event loop
        asyncio.run(run_alert_monitor(uid, SESSION_TOKEN))

    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
