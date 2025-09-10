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
from pathlib import Path
from typing import Dict, List, Set, Tuple

import aiohttp
import requests
from dotenv import load_dotenv
from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage.common import Keys

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
TELEGRAM_BOT_TOKEN = os.getenv("CNBC_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CNBC_SCRAPER_TELEGRAM_GRP")
GMAIL_USERNAME = os.getenv("CNBC_SCRAPER_GMAIL_USERNAME")
GMAIL_PASSWORD = os.getenv("CNBC_SCRAPER_GMAIL_PASSWORD")
LATEST_ASSETS_SHA = os.getenv("CNBC_SCRAPER_LATEST_ASSETS_SHA")
ARTICLE_DATA_SHA = os.getenv("CNBC_SCRAPER_ARTICLE_DATA_SHA")

DATA_DIR = Path("data")
ALERTS_FILE = DATA_DIR / "cnbc_alerts.json"

# NOTE: Only this need to be changed to bypass caching the above 2 sha doesn't change that often
ACCESS_TOKEN = None

# Global variables
last_request_time = 0
browser_page = None


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


def extracte_blockquote_text(article_body):
    if not article_body:
        return None

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
                                for child in element.get("children", []):
                                    text += extract_text(child)
                        return text

                    for element in elements:
                        joined_text += f" {extract_text(element)}"

                    return joined_text.strip()

    return None


"""
async def get_article_data(article_id, uid, access_token):
    global ACCESS_TOKEN

    await rate_limiter.acquire()
    base_url = "https://webql-redesign.cnbcfm.com/graphql"
    variables = {
        "id": article_id,
        "uid": uid,
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

    # FIXME: The token Would expire every month and need to be changed again / find a way to do it within here...
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "authorization": f"Bearer {ACCESS_TOKEN}",
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
                            "Authentication required. Please provide a valid Access token.",
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

                    return extracte_blockquote_text(article_body)
                elif 500 <= response.status < 600:
                    log_message(
                        f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
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
"""


async def send_critical_alert():
    alert = f"🚨 ALERT: Couldn't generate new access token...\nPlease check the server immediately!"
    await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)


async def send_critical_alert_custom(message):
    alert = f"🚨 ALERT: {message}\nPlease check the server immediately!"
    await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)


async def send_login_failed_alert():
    alert = f"🚨 LOGIN FAILED ALERT Login attempt failed!\nWaiting for manual login and script restart."

    for _ in range(3):
        await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        await asyncio.sleep(0.5)


def get_ticker(data):
    match = re.search(r"shares of\s+([A-Z]+),\s+(\w+)\s+its", data)
    if match:
        ticker = match.group(1)
        action_word = match.group(2).lower()
        action = "Buy" if action_word == "increasing" else "Sell"
        return ticker, action
    return None, None


def get_random_cache_buster():
    """Generate a random cache-busting URL variable based on weekday-restricted choices."""
    cache_busters = [
        ("timestamp_uniq", lambda: int(time.time() * 10000)),
        ("request_uuid", lambda: str(uuid.uuid4())),
        ("cache_time", lambda: int(time.time())),
        ("ran_time", lambda: int(time.time() * 1000)),
        ("no_cache_uuid", lambda: str(uuid.uuid4().hex[:16])),
        ("unique", lambda: f"{int(time.time())}-{random.randint(1000, 9999)}"),
        ("req_uuid", lambda: f"req-{uuid.uuid4().hex[:8]}"),
        ("tist", lambda: str(int(time.time()))),
        ("cb_rand", lambda: random.randint(100000, 999999)),
        ("session_id", lambda: uuid.uuid4().hex),
        ("uid", lambda: f"u{random.randint(10000, 99999)}"),
        ("tick_ms", lambda: int(time.time() * 1000)),
        ("cb_uid", lambda: uuid.uuid4().hex[:10]),
        ("cb_tock", lambda: f"{int(time.time())}_{random.randint(0, 999)}"),
        ("zulu_time", lambda: get_current_time().strftime("%Y%m%dT%H%M%SZ")),
        ("cb_xid", lambda: f"xid{random.randint(1000000, 9999999)}"),
        ("uniq_val", lambda: f"val{random.randint(10000, 99999)}"),
        ("meta_time", lambda: f"mt_{int(time.time())}"),
        ("hex_token", lambda: uuid.uuid4().hex[:12]),
        ("burst", lambda: str(int(time.perf_counter() * 1e6))),
        ("ts_hex", lambda: hex(int(time.time()))[2:]),
        ("cb_id", lambda: f"id{random.randint(0, 99999)}"),
        ("time_marker", lambda: f"tm{int(time.time())}"),
        ("ping_id", lambda: f"p{uuid.uuid4().hex[:6]}"),
        ("echo", lambda: f"e{int(time.time()*100)}"),
    ]

    # Determine the weekday (0=Monday, 4=Friday)
    weekday = get_current_time().weekday()
    if weekday >= 5:
        weekday = random.randint(0, 4)

    daily_subset = cache_busters[weekday * 5 : (weekday + 1) * 5]

    variable, value_generator = random.choice(daily_subset)
    return (variable, value_generator())


async def fetch_latest_assets() -> Tuple[List[Dict], str]:
    """Fetch latest alerts from CNBC Investing Club"""
    cache_buster = get_random_cache_buster()
    key = cache_buster[0]

    try:
        base_url = "https://webql-redesign.cnbcfm.com/graphql"

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

        if response_json is None:
            log_message(
                f"Response JSON is None, Raw response: {response.text}", "WARNING"
            )
            return [], key

        if "data" not in response_json or response_json["data"] is None:
            return [], key

        data = response_json["data"]
        if "assetList" not in data or data["assetList"] is None:
            log_message(f"Asset list is None, Response data: {data}", "WARNING")
            return [], key

        asset_list = data["assetList"]
        if "assets" not in asset_list or asset_list["assets"] is None:
            log_message(f"Assets is None, Asset list: {asset_list}", "WARNING")
            return [], key

        return asset_list["assets"], key
    except Exception as e:
        log_message(f"Error fetching alerts: {e}", "ERROR")
        return [], key


async def check_login_status():
    """Check if we're still logged in by visiting the trade alerts page"""
    global browser_page

    try:
        if browser_page is None:
            return False

        log_message("Checking login status...", "INFO")
        browser_page.get("https://www.cnbc.com/investingclub/trade-alerts/")
        await asyncio.sleep(10)  # Wait for page to load

        sign_in_button = browser_page.ele("SIGN IN", timeout=2)
        if "NoneElement" not in str(sign_in_button):
            log_message("Detected we are logged out", "WARNING")
            return False

        log_message("Login status confirmed - still logged in", "INFO")
        return True

    except Exception as e:
        log_message(f"Error checking login status: {e}", "ERROR")
        return False


async def get_article_data_via_browser(article_url):
    """Get article data by navigating to the URL and intercepting the GraphQL response with timeout"""
    global browser_page

    try:
        if browser_page is None:
            log_message("Browser page is None", "ERROR")
            return "Browser page is not available"

        browser_page.listen.start(
            "https://webql-redesign.cnbcfm.com/graphql?operationName=getArticleData"
        )

        log_message(f"Navigating to article: {article_url}", "INFO")
        browser_page.get(article_url)

        article_data = None
        timeout_reached = threading.Event()
        processing_complete = threading.Event()
        result_lock = threading.Lock()

        def process_packets():
            nonlocal article_data
            max_attempts = 5
            attempt = 0

            try:
                for packet in browser_page.listen.steps():
                    if timeout_reached.is_set():
                        break

                    try:
                        attempt += 1
                        if attempt > max_attempts:
                            break

                        if (
                            packet.response
                            and packet.response.extra_info
                            and packet.response.extra_info.all_info
                        ):

                            status_code = packet.response.extra_info.all_info.get(
                                "statusCode"
                            )
                            if packet.response.body and status_code == 200:
                                response_data = packet.response.body
                                # Check if response has valid structure
                                if (
                                    isinstance(response_data, dict)
                                    and "data" in response_data
                                    and response_data.get("data")
                                    and "article" in response_data["data"]
                                ):
                                    # Check authentication
                                    is_authenticated = (
                                        response_data.get("data", {})
                                        .get("article", {})
                                        .get("body", {})
                                        .get("isAuthenticated", False)
                                    )
                                    if not is_authenticated:
                                        log_message(
                                            "Authentication required in intercepted response.",
                                            "WARNING",
                                        )
                                        break

                                    article_body = (
                                        response_data.get("data", {})
                                        .get("article", {})
                                        .get("body", {})
                                        .get("content", [])
                                    )
                                    with result_lock:
                                        article_data = extracte_blockquote_text(
                                            article_body
                                        )
                                    processing_complete.set()
                                    return

                    except Exception as packet_error:
                        log_message(
                            f"Error processing packet {attempt}: {packet_error}",
                            "WARNING",
                        )
                        continue

            except Exception as e:
                log_message(f"Error in packet processing: {e}", "WARNING")
            finally:
                processing_complete.set()

        def check_timeout_and_error_page():
            start_time = time.time()
            timeout_duration = 15  # 15 seconds

            while not processing_complete.is_set():
                elapsed_time = time.time() - start_time

                if elapsed_time >= timeout_duration:
                    asyncio.run(
                        send_critical_alert_custom("Timeout reached after 15 seconds")
                    )
                    timeout_reached.set()

                    # Check for error in page
                    try:
                        error_element = browser_page.ele(
                            "We're sorry, the page you were looking for cannot be found.",
                            timeout=0.5,
                        )
                        if error_element:
                            log_message("Error page detected", "WARNING")
                            return "We're sorry, the page you were looking for cannot be found."
                        else:
                            log_message(
                                "Timeout reached but no error page found", "WARNING"
                            )
                            return "Request timed out after 15 seconds - page may be loading slowly or unavailable"
                    except Exception as e:
                        log_message(f"Error checking for error page: {e}", "WARNING")
                        return "Request timed out after 15 seconds - unable to verify page status"

                time.sleep(0.5)

            return None

        packet_thread = threading.Thread(target=process_packets)
        timeout_thread = threading.Thread(target=check_timeout_and_error_page)

        packet_thread.start()
        timeout_thread.start()

        packet_thread.join(timeout=16)
        timeout_thread.join(timeout=1)

        try:
            browser_page.listen.stop()
        except:
            pass

        with result_lock:
            if article_data is not None:
                return article_data

        try:
            error_element = browser_page.ele(
                "We're sorry, the page you were looking for cannot be found."
            )
            if error_element:
                return "We're sorry, the page you were looking for cannot be found."
        except Exception as e:
            log_message(f"Error checking for error page at end: {e}", "WARNING")

        return (
            "No article data found - page may require authentication or be unavailable"
        )

    except Exception as e:
        log_message(f"Error getting article data via browser: {e}", "ERROR")
        try:
            browser_page.listen.stop()
        except:
            pass
        return f"Error occurred while processing article: {str(e)}"


async def process_article(article, fetch_time):
    try:
        start_time = time.time()
        article_url = article.get("url", "")

        if not article_url:
            log_message(f"No URL found for article {article.get('id')}", "ERROR")
            return False

        article_data = await get_article_data_via_browser(article_url)
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


async def check_for_new_alerts():
    global previous_trade_alerts

    try:
        start = time.time()
        current_articles, key = await fetch_latest_assets()
        fetch_time = time.time() - start
        log_message(f"fetch_latest_assets took {fetch_time:.2f} seconds, with: {key}")

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

                await process_article(article, fetch_time)

        # Save alerts if there were any updates
        if articles_updated:
            save_alerts(previous_trade_alerts)

    except Exception as e:
        log_message(f"Error in check_for_new_alerts: {e}", "ERROR")


async def simulate_human_browser_behavior(page):
    """Simulate human-like browsing behavior"""
    try:
        log_message(
            f"Simulating human browsing behavior...",
            "INFO",
        )

        sleep_interval = 5
        common_pages = [
            "https://www.cnbc.com/tv/",
            "https://www.cnbc.com/markets/",
            "https://www.cnbc.com/personal-finance/",
            "https://www.cnbc.com/technology/",
            "https://www.cnbc.com/pro/analyst-stock-picks/",
        ]

        pages_to_visit = random.sample(common_pages, 3)

        for page_url in pages_to_visit:
            log_message(f"Visiting page: {page_url}", "INFO")
            page.get(page_url)

            scroll_count = random.randint(3, 8)
            for _ in range(scroll_count):
                scroll_amount = random.randint(100, 500)
                page.scroll.down(scroll_amount)

                scroll_pause = random.uniform(0.5, min(2.0, sleep_interval / 5))
                await asyncio.sleep(scroll_pause)

            between_pages_sleep = random.uniform(1, sleep_interval)
            log_message(
                f"Sleeping for {between_pages_sleep:.2f} seconds between pages", "INFO"
            )
            await asyncio.sleep(between_pages_sleep)

        log_message("Human browsing simulation complete", "INFO")
    except Exception as e:
        log_message(f"Error during human simulation: {e}", "WARNING")
    finally:
        page.scroll.to_top()


async def perform_login():
    """Perform login process"""
    global browser_page

    try:
        log_message("Attempting to login to CNBC...")

        if browser_page is None:
            options = ChromiumOptions()
            options.set_argument("--maximize-window")
            browser_page = ChromiumPage(options)

        await simulate_human_browser_behavior(browser_page)

        sign_in_button = browser_page.ele("SIGN IN", timeout=5)
        if "NoneElement" in str(sign_in_button):
            browser_page.ele("css:.SignInMenu-accountMenuAllAccess", timeout=1).click()
            browser_page.ele("css:.AccountSideDrawer-signOutLink", timeout=1).click()

        sign_in_button = browser_page.ele("SIGN IN", timeout=5)
        await asyncio.sleep(random.uniform(1, 2))
        sign_in_button.click()
        await asyncio.sleep(2)

        email_input = browser_page.ele("email", timeout=1)
        password_input = browser_page.ele('css:input[name="password"]', timeout=1)

        if GMAIL_USERNAME is None or GMAIL_PASSWORD is None:
            log_message(f"GMAIL_USERNAME isn't available in the env", "CRITICAL")
            sys.exit(1)

        email_input.clear()
        email_input.input(GMAIL_USERNAME)

        password_input.clear()
        await asyncio.sleep(2)
        password_input.input(GMAIL_PASSWORD + Keys.ENTER)
        await asyncio.sleep(3)

        sign_in_ele = browser_page.ele('css:button[name="signin"]', timeout=5)
        if "NoneElement" not in str(sign_in_ele):
            await asyncio.sleep(5)
            sign_in_ele.click()

        await asyncio.sleep(5)
        if await check_login_status():
            log_message("Login successful!", "INFO")
            return True
        else:
            log_message("Login failed", "ERROR")
            return False

    except Exception as e:
        log_message(f"Login attempt failed: {e}", "ERROR")
        return False


async def ensure_logged_in():
    """Ensure we are logged in, attempt login if needed"""
    if await check_login_status():
        return True

    if await perform_login():
        return True
    else:
        await send_login_failed_alert()
        log_message(
            "Waiting for manual intervention...",
            "CRITICAL",
        )

        while True:
            await asyncio.sleep(300)
            if await check_login_status():
                log_message("Manual login detected! Resuming operations...", "INFO")
                return True


async def run_alert_monitor():
    global previous_trade_alerts, browser_page

    while True:
        try:
            await sleep_until_market_open()
            await initialize_websocket()

            if browser_page is None:
                options = ChromiumOptions()
                options.set_argument("--maximize-window")
                browser_page = ChromiumPage(options)

            if not await ensure_logged_in():
                log_message("Could not ensure login. Retrying in 5 minutes...", "ERROR")
                await asyncio.sleep(300)
                continue

            log_message(
                "Market is open and logged in. Starting to check for new blog posts...",
                "DEBUG",
            )

            _, _, market_close_time = get_next_market_times()
            previous_trade_alerts = load_saved_alerts()

            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                try:
                    await check_for_new_alerts()
                    await asyncio.sleep(0.2)

                except Exception as e:
                    log_message(f"Error checking alerts: {e}", "ERROR")
                    await asyncio.sleep(5)

        except Exception as e:
            log_message(f"Error in monitor loop: {e}", "ERROR")
            await asyncio.sleep(5)


def main():
    if not all(
        [
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            GMAIL_USERNAME,
            GMAIL_PASSWORD,
            LATEST_ASSETS_SHA,
            ARTICLE_DATA_SHA,
        ]
    ):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        # Start the async event loop
        asyncio.run(run_alert_monitor())

    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
