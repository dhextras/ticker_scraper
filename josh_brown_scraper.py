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
from utils.ticker_deck_sender import initialize_ticker_deck, send_ticker_deck_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("JOSH_BROWN_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("JOSH_BROWN_TELEGRAM_GRP")
GMAIL_USERNAME = os.getenv("JOSH_BROWN_GMAIL_USERNAME")
GMAIL_PASSWORD = os.getenv("JOSH_BROWN_GMAIL_PASSWORD")
LATEST_ASSETS_SHA = os.getenv("CNBC_SCRAPER_LATEST_ASSETS_SHA")
ARTICLE_DATA_SHA = os.getenv("CNBC_SCRAPER_ARTICLE_DATA_SHA")

DATA_DIR = Path("data")
CRED_FILE = "cred/josh_creds.json"
ALERTS_FILE = DATA_DIR / "josh_brown_alerts.json"

# NOTE: Only this need to be changed to bypass caching the above 2 sha doesn't change that often
ACCESS_TOKEN = None

# Global variables
last_request_time = 0


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


def extract_text_from_element(element):
    """Recursively extracts text from an element and its children."""
    if isinstance(element, str):
        return element
    if not isinstance(element, dict):
        return ""
    tag_name = element.get("tagName", "")
    if tag_name in ["cnbcvideo", "infographic", "image"]:
        return ""
    text = ""
    children = element.get("children", [])
    for child in children:
        child_text = extract_text_from_element(child)
        if child_text:
            text += child_text + " "
    if tag_name == "subtitle":
        text = f"\n=== {text.strip()} ===\n"
    elif tag_name == "p":
        text = text.strip() + "\n"
    elif tag_name == "blockquote":
        text = f"\n> {text.strip()}\n"
    return text


def extract_ticker_from_headlines(body_content):
    """Looks for 'New Addition' or 'Best Stock Spotlight' or 'Best Stocks Spotlight' with ticker in parentheses."""
    for content_block in body_content:
        if content_block.get("tagName") == "subtitle":
            children = content_block.get("children", [])
            subtitle_text = ""
            for child in children:
                if isinstance(child, str):
                    subtitle_text += child + " "
                elif isinstance(child, dict):
                    subtitle_text += extract_text_from_element(child) + " "
            subtitle_text = subtitle_text.strip()
            patterns = [
                r"New Addition:\s*([^(]+)\s*\(([A-Z]{1,5})\)",
                r"Best Stock Spotlight:\s*([^(]+)\s*\(([A-Z]{1,5})\)",
                r"Best Stocks Spotlight:\s*([^(]+)\s*\(([A-Z]{1,5})\)",
            ]
            for pattern in patterns:
                match = re.search(pattern, subtitle_text, re.IGNORECASE)
                if match:
                    return {
                        "headline": subtitle_text.strip(),
                        "ticker": match.group(2).strip(),
                        "company": match.group(1).strip(),
                    }
    return None


def extract_full_text_content(body_content):
    """Extracts all text content from the article body."""
    full_text = ""
    for content_block in body_content:
        text = extract_text_from_element(content_block)
        if text:
            full_text += text + "\n\n"
    return full_text.strip()


async def get_article_data(article_id, uid, access_token):
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
        "authorization": f"Bearer {access_token}",
    }

    encoded_url = f"{base_url}?{urllib.parse.urlencode(params)}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(encoded_url, headers=headers) as response:
                if response.status == 200:
                    response_json = await response.json()

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

                    body_content = (
                        response_json.get("data", {})
                        .get("article", {})
                        .get("body", {})
                        .get("content", [])
                    )

                    return body_content
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


async def send_critical_alert():
    alert = f"🚨 ALERT: Couldn't generate new access token...\nPlease check the server immediately!"
    await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)


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

    weekday = get_current_time().weekday()
    if weekday >= 5:
        weekday = random.randint(0, 4)

    daily_subset = cache_busters[weekday * 5 : (weekday + 1) * 5]

    variable, value_generator = random.choice(daily_subset)
    return (variable, value_generator())


async def fetch_latest_assets() -> Tuple[List[Dict], str]:
    """Fetch latest alerts from Josh Brown"""
    cache_buster = get_random_cache_buster()
    key = cache_buster[0]

    try:
        base_url = "https://webql-redesign.cnbcfm.com/graphql"

        variables = {
            "id": "100831613",
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


async def process_article(article, uid, access_token, fetch_time):
    try:
        start_time = time.time()
        body_content = await get_article_data(article.get("id"), uid, access_token)
        fetch_data_time = time.time() - start_time

        if body_content:
            published_date = datetime.strptime(
                article["datePublished"], "%Y-%m-%dT%H:%M:%S%z"
            )
            article_timezone = published_date.tzinfo
            current_time = get_current_time().astimezone(article_timezone)

            ticker_info = extract_ticker_from_headlines(body_content)
            full_content = extract_full_text_content(body_content)

            log_message(
                f"Time difference: {(current_time - published_date).total_seconds():.2f} seconds",
                "INFO",
            )

            message = (
                f"<b>New Josh Brown Alert!</b>\n"
                f"<b>Published Date:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                f"<b>Time difference:</b> {(current_time - published_date).total_seconds():.2f} seconds\n"
                f"<b>Assets, Article Data fetch time:</b> {fetch_time:.2f}s, {fetch_data_time:.2f}s\n"
                f"<b>ID:</b> {article['id']}\n"
                f"<b>Title:</b> {article['title']}\n"
            )

            if ticker_info:
                await send_ws_message(
                    {
                        "name": "Josh Brown",
                        "type": "Buy",
                        "ticker": ticker_info["ticker"],
                        "sender": "josh_brown",
                    },
                )
                message += f"\n<b>Ticker:</b> {ticker_info['ticker']} - {ticker_info['company']}\n"
                message += f"<b>Headline:</b> {ticker_info['headline']}\n"
            else:
                await send_ticker_deck_message(
                    sender="josh_brown",
                    name="Josh Brown",
                    content=full_content,
                )

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            return True
    except Exception as e:
        log_message(f"Error processing article {article.get('id')}: {e}", "ERROR")
    return False


async def check_for_new_alerts(uid, access_token):
    global previous_trade_alerts

    try:
        start = time.time()
        current_articles, key = await fetch_latest_assets()
        fetch_time = time.time() - start
        log_message(f"fetch_latest_assets took {fetch_time:.2f} seconds, with: {key}")

        articles_updated = False

        for article in current_articles:
            article_id = article["id"]
            article_type = article["type"]

            if (
                article_id not in previous_trade_alerts
                and article_type == "cnbcnewsstory"
            ):
                previous_trade_alerts.add(article_id)
                articles_updated = True

                await process_article(article, uid, access_token, fetch_time)

        if articles_updated:
            save_alerts(previous_trade_alerts)

    except Exception as e:
        log_message(f"Error in check_for_new_alerts: {e}", "ERROR")


async def run_alert_monitor(uid):
    global previous_trade_alerts
    global ACCESS_TOKEN

    while True:
        try:
            await sleep_until_market_open()
            await initialize_websocket()
            await initialize_ticker_deck("Josh Brown")

            if not ACCESS_TOKEN:
                await get_new_access_token()
                if not ACCESS_TOKEN:
                    await send_critical_alert()

            log_message(
                "Market is open. Starting to check for new Josh Brown posts...", "DEBUG"
            )

            _, _, market_close_time = get_next_market_times()

            previous_trade_alerts = load_saved_alerts()

            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    ACCESS_TOKEN = None
                    break

                try:
                    await check_for_new_alerts(uid, ACCESS_TOKEN)
                    await asyncio.sleep(0.2)

                except Exception as e:
                    log_message(f"Error checking alerts: {e}", "ERROR")
                    await asyncio.sleep(5)

        except Exception as e:
            log_message(f"Error in monitor loop: {e}", "ERROR")
            await asyncio.sleep(5)


async def get_new_access_token():
    global ACCESS_TOKEN
    max_retries = 5

    for attempt in range(max_retries):
        options = ChromiumOptions()
        options.set_argument("--no-sandbox")
        options.set_argument("--disable-dev-shm-usage")
        options.set_argument("--disable-gpu")
        options.set_argument("--maximize-window")
        options.set_argument("--disable-search-engine-choice-screen")
        options.set_argument("--blink-settings=imagesEnabled=false")
        page = ChromiumPage()
        page.clear_cache()

        try:
            log_message(f"Login attempt {attempt + 1}/{max_retries}")
            page.listen.start(
                "https://registerng.cnbc.com/api/v4/client/201/users/signin"
            )
            log_message("Trying to login to cnbc...")
            page.get("https://www.cnbc.com/investingclub/trade-alerts/")
            await asyncio.sleep(random.uniform(2, 4))

            scroll_pause_time = random.uniform(1, 2)
            for _ in range(3):
                page.scroll.down(random.randint(80, 300))
                await asyncio.sleep(scroll_pause_time)
            page.scroll.to_top()
            await asyncio.sleep(scroll_pause_time)

            async def login():
                try:
                    sign_in_button = page.ele("SIGN IN", timeout=5)
                    await asyncio.sleep(random.uniform(1, 2))
                    sign_in_button.click()
                    await asyncio.sleep(2)

                    email_input = page.ele("email", timeout=1)
                    password_input = page.ele('css:input[name="password"]', timeout=1)

                    if GMAIL_USERNAME is None or GMAIL_PASSWORD is None:
                        log_message(
                            f"GMAIL_USERNAME isn't available in the env", "CRITICAL"
                        )
                        sys.exit(1)

                    email_input.clear()
                    email_input.input(GMAIL_USERNAME)

                    password_input.clear()
                    await asyncio.sleep(2)
                    password_input.input(GMAIL_PASSWORD + Keys.ENTER)
                    await asyncio.sleep(3)

                    sign_in_ele = page.ele('css:button[name="signin"]', timeout=5)
                    if "NoneElement" not in str(sign_in_ele):
                        sign_in_ele.click()

                    return True
                except Exception as login_error:
                    log_message(f"Login attempt failed: {login_error}", "WARNING")
                    return False

            login_success = await login()
            if not login_success:
                log_message(f"Login failed on attempt {attempt + 1}", "WARNING")
                raise Exception("Login failed")

            token_found = False

            i = 0
            for packet in page.listen.steps():
                try:
                    if (
                        packet.response
                        and packet.response.extra_info
                        and packet.response.extra_info.all_info
                    ):
                        status_code = packet.response.extra_info.all_info.get(
                            "statusCode"
                        )

                        if packet.response.body:
                            access_token = packet.response.body.get("data", {}).get(
                                "access_token", None
                            )

                            if access_token and status_code == 200:
                                log_message(
                                    f"Successfully intercepted Access Token: {access_token[:30]}...",
                                    "INFO",
                                )
                                ACCESS_TOKEN = access_token
                                token_found = True
                                return
                            else:
                                log_message(
                                    f"Invalid token or status code. Status: {status_code}, Token present: {access_token is not None}",
                                    "WARNING",
                                )

                except Exception as packet_error:
                    log_message(f"Error processing packet: {packet_error}", "WARNING")
                    continue

                # NOTE: This is just here to break the page.liste.steps() from waiting forever
                if i >= 1:
                    break
                i += 1

            page.listen.stop()

            if not token_found:
                raise Exception("No valid access token found in response")

        except Exception as e:
            log_message(f"Attempt {attempt + 1} failed: {e}", "ERROR")

            try:
                page.clear_cache()
                page.quit()
            except:
                pass

            if attempt < max_retries - 1:
                wait_time = random.uniform(3, 6)
                log_message(f"Waiting {wait_time:.1f} seconds before retry...", "INFO")
                await asyncio.sleep(wait_time)
            else:
                log_message(f"All {max_retries} login attempts failed", "ERROR")
                log_message(f"Using existing access token: {ACCESS_TOKEN}", "INFO")
                await send_critical_alert()
                return
        finally:
            try:
                page.clear_cache()
                page.quit()
            except:
                pass


def main():
    uid = GMAIL_USERNAME

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
        asyncio.run(run_alert_monitor(uid))

    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
