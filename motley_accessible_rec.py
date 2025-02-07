import asyncio
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta
from uuid import uuid4

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumrequests import Chrome

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("FOOL_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("FOOL_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
FOOL_USERNAME = os.getenv("FOOL_USERNAME")
FOOL_PASSWORD = os.getenv("FOOL_PASSWORD")
FOOL_API_KEY = os.getenv("FOOL_API_KEY")
FOOL_GRAPHQL_HASH = os.getenv("FOOL_GRAPHQL_HASH")
CREDS_PATH = "cred/fool_session.json"
SELECTED_INSTRUMENTS_FILE = "data/motley_selected_instruments.json"
INSTRUMENT_DATA_FILE = "data/motley_accessible_instrument_data.json"

os.makedirs("cred", exist_ok=True)
os.makedirs("data", exist_ok=True)

PRODUCT_NAMES = {
    1081: "Stock Advisor",
    1069: "Rule Breakers",
    4198: "Hidden Gems",
    4488: "Dividend Investor",
}


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


def load_selected_ids():
    try:
        with open(SELECTED_INSTRUMENTS_FILE, "r") as f:
            return json.load(f)
    except:
        return []


def load_instrument_data():
    try:
        with open(INSTRUMENT_DATA_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_instrument_data(data):
    with open(INSTRUMENT_DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_random_cache_buster():
    cache_busters = [
        ("cache_timestamp", lambda: int(time.time() * 10000)),
        ("request_uuid", lambda: str(uuid.uuid4())),
        ("cache_time", lambda: int(time.time())),
        ("ran_time", lambda: int(time.time() * 1000)),
        ("no_cache_uuid", lambda: str(uuid.uuid4().hex[:16])),
        ("unique", lambda: f"{int(time.time())}-{random.randint(1000, 9999)}"),
        ("req_uuid", lambda: f"req-{uuid.uuid4().hex[:8]}"),
        ("tist", lambda: str(int(time.time()))),
    ]
    variable, value_generator = random.choice(cache_busters)
    return f"{variable}={value_generator()}"


async def get_api_session(driver):
    try:
        driver.get("https://www.fool.com/premium/api/auth/session")
        soup = BeautifulSoup(driver.page_source, "html.parser")
        json_data = soup.find("pre").text
        return json.loads(json_data)
    except Exception as e:
        log_message(f"Error getting API session token: {e}", "ERROR")
        return None


async def get_session_cookie(driver):
    try:
        cookies = driver.get_cookies()
        for cookie in cookies:
            if cookie["name"] == "__Secure-authjs.session-token":
                return cookie["value"]
        return None
    except Exception as e:
        log_message(f"Error getting session cookie: {e}", "ERROR")
        return None


async def get_new_session_token():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--maximize-window")
    options.add_argument("--disable-search-engine-choice-screen")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    driver = Chrome(options=options)
    driver.get("https://www.fool.com/premium/")
    time.sleep(random.uniform(2, 4))

    try:
        current_url = driver.current_url
        email_input = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "usernameOrEmail"))
        )
        email_input.send_keys(FOOL_USERNAME)
        password_input = driver.find_element(By.ID, "password")
        password_input.send_keys(FOOL_PASSWORD)

        submit_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        submit_button.click()

        for _ in range(10):
            time.sleep(1)
            if current_url != driver.current_url:
                break

        while current_url == driver.current_url:
            log_message("Failed to login, waiting for manual login....", "WARNING")
            time.sleep(5)

        api_session = await get_api_session(driver)
        session_token = await get_session_cookie(driver)

        if api_session and session_token:
            session_data = {
                "accessToken": api_session.get("accessToken", None),
                "session_token": session_token,
                "expires": (datetime.now(pytz.UTC) + timedelta(days=1)).isoformat(),
            }
            save_session_credentials(session_data)
            return session_data

        return None

    except Exception as e:
        log_message(f"Error in login process: {e}", "ERROR")
        return None
    finally:
        driver.quit()


async def fetch_instrument_data(session_data, ids):
    await rate_limiter.acquire()
    cache_uuid = uuid4()
    timestamp = int(time.time() * 10000)
    cache_buster = get_random_cache_buster()

    base_url = f"https://api.fool.com/premium-graphql-proxy/graphql?{cache_buster}"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {session_data['accessToken']}",
        "Apikey": FOOL_API_KEY,
        "cache-control": "no-cache, no-store, max-age=0, must-revalidate, private",
        "pragma": "no-cache",
        "cache-timestamp": str(timestamp),
        "cache-uuid": str(cache_uuid),
    }

    query = """
    query GetFoolRecommendations($ids: [ID!]!) {
        instruments(ids: $ids) {
            instrumentId
            symbol
            name
            accessibleFoolRecommendations {
                actionDate
                content {
                    productId
                    publishAt
                    url
                }
            }
        }
    }
    """

    variables = {"ids": ids}
    payload = {
        "operationName": "GetFoolRecommendations",
        "query": query,
        "variables": variables,
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                base_url, json=payload, headers=headers
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data", {}).get("instruments", [])
                else:
                    log_message(
                        f"Error fetching instrument data: {response.status}", "ERROR"
                    )
                    return []
        except Exception as e:
            log_message(f"Error in fetch_instrument_data: {e}", "ERROR")
            return []


def load_session_credentials():
    try:
        if os.path.exists(CREDS_PATH):
            with open(CREDS_PATH, "r") as f:
                creds = json.load(f)
                if datetime.fromisoformat(
                    creds["expires"].replace("Z", "+00:00")
                ) > datetime.now(pytz.UTC):
                    return creds
    except Exception as e:
        log_message(f"Error loading credentials: {e}", "ERROR")
    return None


def save_session_credentials(creds):
    try:
        os.makedirs(os.path.dirname(CREDS_PATH), exist_ok=True)
        with open(CREDS_PATH, "w") as f:
            json.dump(creds, f, indent=2)
    except Exception as e:
        log_message(f"Error saving credentials: {e}", "ERROR")


async def process_new_recommendations(instrument, stored_data):
    instrument_id = str(instrument["instrumentId"])
    symbol = instrument["symbol"]
    name = instrument["name"]

    # FIXME: Add up filtering for the new alert here or after fetching if needed
    if instrument_id not in stored_data:
        stored_data[instrument_id] = []

    current_time = datetime.now(pytz.UTC)
    stored_urls = {rec["url"] for rec in stored_data[instrument_id]}

    for recommendation in instrument["accessibleFoolRecommendations"]:
        content = recommendation["content"]
        url = content["url"]

        if url not in stored_urls:
            product_id = content["productId"]
            publish_at = datetime.fromisoformat(
                content["publishAt"].replace("Z", "+00:00")
            )
            published_formatted = str(publish_at.strftime("%Y-%m-%d %H:%M:%S %Z"))

            action_date = recommendation["actionDate"]

            stored_data[instrument_id].append(
                {
                    "url": url,
                    "productId": product_id,
                    "publishAt": published_formatted,
                    "actionDate": action_date,
                }
            )

            product_name = PRODUCT_NAMES.get(product_id, "Unknown")

            message = (
                f"<b>New {product_name} Accessible Recommendation!</b>\n"
                f"<b>Company:</b> {name} ({symbol})\n"
                f"<b>Action Date:</b> {action_date}\n"
                f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>Published Date:</b> {published_formatted}\n"
                f"<b>URL:</b> {url}\n"
            )

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

            await send_ws_message(
                {
                    "name": f"{product_name} - Accessible Rec",
                    "type": "Buy",
                    "ticker": symbol,
                    "sender": "motley_fool",
                    "target": "CSS",
                },
                WS_SERVER_URL,
            )

            log_message(f"New recommendation found for {symbol}, url: {url}", "INFO")

    return stored_data


async def check_for_new_recommendations(ids, session_data):
    try:
        random.shuffle(ids)
        stored_data = load_instrument_data()
        start_time = time.time()

        instruments = await fetch_instrument_data(session_data, ids)

        duration = time.time() - start_time
        log_message(
            f"check_for_new_recommendations took {duration:.2f} seconds. Total Articles: {len(instruments)}",
            "INFO",
        )

        if instruments:
            for instrument in instruments:
                stored_data = await process_new_recommendations(instrument, stored_data)

            save_instrument_data(stored_data)

    except Exception as e:
        log_message(f"Error in check_for_new_recommendations: {e}", "ERROR")


async def run_monitor():
    ids = load_selected_ids()
    session_data = load_session_credentials()

    if not session_data:
        session_data = await get_new_session_token()
        if not session_data:
            log_message("Failed to get session token", "CRITICAL")
            return

    while True:
        try:
            await sleep_until_market_open()
            log_message(
                "Market is open. Starting to check for new recommendations...", "DEBUG"
            )

            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = datetime.now(pytz.timezone("America/New_York"))
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                if datetime.fromisoformat(
                    session_data["expires"].replace("Z", "+00:00")
                ) < datetime.now(pytz.UTC):
                    session_data = await get_new_session_token()
                    if not session_data:
                        raise Exception("Failed to refresh session token")

                await check_for_new_recommendations(ids, session_data)
                await asyncio.sleep(1)

        except Exception as e:
            log_message(f"Error in monitor loop: {e}", "ERROR")
            await asyncio.sleep(5)


def main():
    if not all(
        [
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            WS_SERVER_URL,
            FOOL_USERNAME,
            FOOL_PASSWORD,
            FOOL_API_KEY,
            FOOL_GRAPHQL_HASH,
            CREDS_PATH,
        ]
    ):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_monitor())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
