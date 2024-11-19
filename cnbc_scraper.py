import asyncio
import json
import os
import random
import sys
import time
import urllib.parse
from datetime import datetime
from functools import wraps
from typing import Dict, List, Tuple

import aiohttp
import pytz
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
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("CNBC_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CNBC_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
GMAIL_USERNAME = os.getenv("CNBC_SCRAPER_GMAIL_USERNAME")
GMAIL_PASSWORD = os.getenv("CNBC_SCRAPER_GMAIL_PASSWORD")
LATEST_ARTICLE_SHA = os.getenv("CNBC_SCRAPER_LATEST_ARTICLE_SHA")
ARTICLE_DATA_SHA = os.getenv("CNBC_SCRAPER_ARTICLE_DATA_SHA")
SESSION_TOKEN = os.getenv("CNBC_SCRAPER_SESSION_TOKEN")

# Global variables
previous_articles = []
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

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(base_url, params=params) as response:
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
                                        paragraph = child.get("children", [])[0]
                                        if paragraph.get("tagName") == "p":
                                            text = "".join(
                                                [
                                                    (
                                                        part
                                                        if isinstance(part, str)
                                                        else part.get("children", [])[0]
                                                    )
                                                    for part in paragraph.get(
                                                        "children", []
                                                    )
                                                ]
                                            )
                                            return text
                    return None
                else:
                    log_message(
                        f"Error fetching article data: {response.status}", "ERROR"
                    )
                    return None
        except Exception as e:
            log_message(f"Exception in get_article_data: {e}", "ERROR")
            return None


async def process_response(response_json: Dict, method_name: str) -> Tuple[List, int]:
    """Process the API response and return alerts with count."""
    trade_alerts = []
    alert_ids = set()

    dtc_notifications = response_json.get("data", {}).get("dtcNotifications", {})

    if dtc_notifications:
        trade_alerts_raw = dtc_notifications.get("tradeAlerts", [])
        if trade_alerts_raw:
            log_message(
                f"[{method_name}] Found {len(trade_alerts_raw)} trade alerts", "INFO"
            )
            for alert in trade_alerts_raw:
                if alert.get("id") not in alert_ids:
                    trade_alerts.append(alert)
                    alert_ids.add(alert.get("id"))

    assets = []
    news_items = dtc_notifications.get("news", [])
    if news_items:
        log_message(f"[{method_name}] Processing {len(news_items)} news items", "INFO")
        for item in news_items:
            asset = item.get("asset")
            if asset and asset.get("section", {}).get("id") == 106983829:
                asset_id = asset.get("id")
                if asset_id not in alert_ids:
                    assets.append(
                        {
                            "id": asset_id,
                            "title": asset.get("title"),
                            "type": asset.get("type"),
                            "tickerSymbols": asset.get("tickerSymbols"),
                            "dateLastPublished": asset.get("dateLastPublished"),
                            "url": asset.get("url"),
                            "contentClassification": asset.get("contentClassification"),
                            "section": asset.get("section", {}).get("title"),
                        }
                    )
                    alert_ids.add(asset_id)

    combined_alerts = trade_alerts + assets
    log_message(
        f"[{method_name}] Total combined alerts: {len(combined_alerts)}", "INFO"
    )
    return combined_alerts, len(combined_alerts)


async def fetch_latest_articles(uid: str, session_token: str) -> List[Dict]:
    """Fetch articles using multiple methods and compare results."""
    await rate_limiter.acquire()
    log_message("Starting article fetch with multiple methods", "INFO")

    base_url = "https://webql-redesign.cnbcfm.com/graphql"
    variables = {"hasICAccess": True, "uid": uid, "sessionToken": session_token}
    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": LATEST_ARTICLE_SHA,
        }
    }
    params = {
        "operationName": "notifications",
        "variables": json.dumps(variables),
        "extensions": json.dumps(extensions),
    }

    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-store",
        "priority": "u=1, i",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "same-origin",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    # Create encoded URL
    encoded_url = f"{base_url}?{urllib.parse.urlencode(params)}"
    all_alerts = []

    # Method 2: aiohttp with headers
    log_message("Trying aiohttp params with headers", "INFO")
    async with aiohttp.ClientSession() as session:
        try:
            start_time = time.time()
            async with session.get(
                base_url, params=params, headers=headers
            ) as response:
                if response.status == 200:
                    response_json = await response.json()
                    alerts, count = await process_response(
                        response_json, "aiohttp_headers"
                    )
                    elapsed = time.time() - start_time
                    log_message(
                        f"aiohttp_headers completed in {elapsed:.2f}s with {count} alerts",
                        "ERROR",
                    )
                    all_alerts.extend(alerts)
                else:
                    log_message(
                        f"aiohttp_headers failed with status {response.status}", "ERROR"
                    )
        except Exception as e:
            log_message(f"Error in aiohttp_headers: {str(e)}", "ERROR")

    await asyncio.sleep(1)
    # Method 3: aiohttp with direct URL
    log_message("Trying aiohttp with direct URL", "INFO")
    async with aiohttp.ClientSession() as session:
        try:
            start_time = time.time()
            async with session.get(encoded_url, headers=headers) as response:
                if response.status == 200:
                    response_json = await response.json()
                    alerts, count = await process_response(
                        response_json, "aiohttp_direct"
                    )
                    elapsed = time.time() - start_time
                    log_message(
                        f"aiohttp_direct completed in {elapsed:.2f}s with {count} alerts",
                        "ERROR",
                    )
                    all_alerts.extend(alerts)
                else:
                    log_message(
                        f"aiohttp_direct failed with status {response.status}", "ERROR"
                    )
        except Exception as e:
            log_message(f"Error in aiohttp_direct: {str(e)}", "ERROR")

    await asyncio.sleep(1)
    # Method 5: requests with headers
    log_message("Trying requests params with headers", "INFO")
    try:
        start_time = time.time()
        response = requests.get(base_url, params=params, headers=headers)
        if response.status_code == 200:
            response_json = response.json()
            alerts, count = await process_response(response_json, "requests_headers")
            elapsed = time.time() - start_time
            log_message(
                f"requests_headers completed in {elapsed:.2f}s with {count} alerts",
                "ERROR",
            )
            all_alerts.extend(alerts)
        else:
            log_message(
                f"requests_headers failed with status {response.status_code}", "ERROR"
            )
    except Exception as e:
        log_message(f"Error in requests_headers: {str(e)}", "ERROR")

    await asyncio.sleep(1)
    # Method 6: requests with direct URL
    log_message("Trying requests with direct URL", "INFO")
    try:
        start_time = time.time()
        response = requests.get(encoded_url, headers=headers)
        if response.status_code == 200:
            response_json = response.json()
            alerts, count = await process_response(response_json, "requests_direct")
            elapsed = time.time() - start_time
            log_message(
                f"requests_direct completed in {elapsed:.2f}s with {count} alerts",
                "ERROR",
            )
            all_alerts.extend(alerts)
        else:
            log_message(
                f"requests_direct failed with status {response.status_code}", "ERROR"
            )
    except Exception as e:
        log_message(f"Error in requests_direct: {str(e)}", "ERROR")

    # Remove duplicates
    seen_ids = set()
    unique_alerts = []
    for alert in all_alerts:
        if alert["id"] not in seen_ids:
            unique_alerts.append(alert)
            seen_ids.add(alert["id"])

    log_message(
        f"Fetch complete. Found {len(unique_alerts)} unique alerts from all methods",
        "INFO",
    )
    return unique_alerts


async def process_article(article, uid, session_token):
    try:
        article_data = await get_article_data(article.get("id"), uid, session_token)
        if article_data:
            article["article_data"] = article_data
            published_date = datetime.strptime(
                article["dateLastPublished"], "%Y-%m-%dT%H:%M:%S%z"
            )
            article_timezone = published_date.tzinfo

            await send_ws_message(
                {
                    "name": "CNBC",
                    "type": "Buy",
                    "ticker": f"NO_TICKER_IGNORE - Title: {article['title']}",
                    "sender": "cnbc",
                },
                WS_SERVER_URL,
            )

            current_time = datetime.now(pytz.utc).astimezone(article_timezone)
            log_message(
                f"Time difference: {(current_time - published_date).total_seconds():.2f} seconds",
                "ERROR",
            )
            message = (
                f"<b>New Article Alert!</b>\n"
                f"<b>Published Date:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                f"<b>Title:</b> {article['title']}\n"
                f"<b>Content:</b> {article['article_data']}\n"
            )

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            return True
    except Exception as e:
        log_message(f"Error processing article {article.get('id')}: {e}", "ERROR")
    return False


@timing_decorator
async def check_for_new_alerts(prev_articles, uid, session_token):
    try:
        current_articles = await fetch_latest_articles(uid, session_token)

        if not current_articles:
            return prev_articles, []

        new_articles = []
        known_ids = {article.get("id") for article in prev_articles}

        for article in current_articles:
            article_id = article.get("id")
            if article_id and article_id not in known_ids:
                new_articles.append(article)

        if new_articles:
            processing_tasks = [
                process_article(article, uid, session_token) for article in new_articles
            ]
            await asyncio.gather(*processing_tasks)

        return current_articles, new_articles

    except Exception as e:
        log_message(f"Error in check_for_new_alerts: {e}", "ERROR")
        return prev_articles, []


async def run_alert_monitor(uid, session_token):
    prev_articles = []

    while True:
        try:
            # Wait until market open
            await sleep_until_market_open()
            log_message("Market is open. Starting to check for new blog posts...")

            # Get market close time
            _, _, market_close_time = get_next_market_times()

            # Main market hours loop
            while True:
                current_time = datetime.now(pytz.timezone("America/New_York"))
                if current_time > market_close_time:
                    log_message("Market is closed. Waiting for next market open...")
                    break

                start_time = time.time()

                try:
                    prev_articles, new_articles = await check_for_new_alerts(
                        prev_articles, uid, session_token
                    )

                    if new_articles:
                        log_message(f"Found {len(new_articles)} new articles", "INFO")

                    execution_time = time.time() - start_time
                    log_message(
                        f"Total iteration time: {execution_time:.2f} seconds\n", "INFO"
                    )

                    # Adaptive sleep based on execution time
                    await asyncio.sleep(min(2, 2 - execution_time))

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
            LATEST_ARTICLE_SHA,
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
