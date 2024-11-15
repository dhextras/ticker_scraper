import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta

import aiohttp
import pytz
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

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("FOOL_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("FOOL_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
FOOL_USERNAME = os.getenv("FOOL_USERNAME")
FOOL_PASSWORD = os.getenv("FOOL_PASSWORD")
FOOL_API_KEY = os.getenv("FOOL_API_KEY")
FOOL_GRAPHQL_HASH = os.getenv("FOOL_GRAPHQL_HASH")
CREDS_PATH = "cred/fool_session.json"
PROCESSED_URLS_FILE = "data/motley_processed_urls.json"

os.makedirs("cred", exist_ok=True)
os.makedirs("data", exist_ok=True)

# Global variables
previous_articles = []
last_request_time = 0
MIN_REQUEST_INTERVAL = 1
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


def load_processed_urls():
    try:
        with open(PROCESSED_URLS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_urls(urls):
    with open(PROCESSED_URLS_FILE, "w") as f:
        json.dump(list(urls), f)
    log_message("Processed URLs saved.", "INFO")


async def get_api_session(driver):
    """Fetch the API session token for GraphQL requests"""
    try:
        session_response = driver.request(
            "GET", "https://www.fool.com/premium/api/auth/session"
        )
        if session_response.status_code == 200:
            return session_response.json()
        else:
            log_message(
                f"Failed to get API session token: {session_response.status_code}",
                "ERROR",
            )
            return None
    except Exception as e:
        log_message(f"Error getting API session token: {e}", "ERROR")
        return None


def get_browser_session(driver):
    """Get browser session data (cookies and localStorage)"""
    try:
        cookies = driver.get_cookies()
        local_storage = driver.execute_script("return window.localStorage;")
        return {
            "cookies": cookies,
            "localStorage": local_storage,
        }
    except Exception as e:
        log_message(f"Error getting browser session: {e}", "ERROR")
        return None


async def get_new_session_token():
    options = Options()
    options.add_argument("--headless")  # Comment out for first-time setup
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
        email_input = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "usernameOrEmail"))
        )
        email_input.send_keys(FOOL_USERNAME)
        password_input = driver.find_element(By.ID, "password")
        password_input.send_keys(FOOL_PASSWORD)

        submit_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        submit_button.click()

        time.sleep(5)

        browser_session = get_browser_session(driver)
        api_session = await get_api_session(driver)

        if browser_session and api_session:
            session_data = {
                **browser_session,
                **api_session,
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


def create_session_with_cookies(cookies):
    """Create aiohttp ClientSession with saved cookies"""
    session = aiohttp.ClientSession()
    for cookie in cookies:
        session.cookie_jar.update_cookies({cookie["name"]: cookie["value"]})
    return session


async def fetch_latest_articles(session_data):
    await rate_limiter.acquire()

    base_url = "https://api.fool.com/premium-graphql-proxy/graphql"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {session_data['accessToken']}",
        "Apikey": FOOL_API_KEY,
        "Cache-Control": "max-age=0",
    }

    variables = {
        "tagsFilterType": "OR",
        "videoInclusion": "INCLUDE",
        "includeImages": False,
        "includeFreeContent": False,
        "includeStaticPages": True,
        "limit": 10,
        "offset": 0,
        "tags": [],
        "tagsExcluded": [],
        "authorIds": [],
        "myStocks": False,
        "productIds": [
            1081,  # Stock Advisor
            1069,  # Rule Breakers
            4198,  # Hidden gems
            4488,  # Dividend Investor
        ],
        "orderBy": "",
        "authorIdsExcluded": [],
    }
    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": FOOL_GRAPHQL_HASH,
        }
    }
    params = {
        "operationName": "FilteredArticleList",
        "variables": json.dumps(variables),
        "extensions": json.dumps(extensions),
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                base_url,
                params=params,
                headers=headers,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data", {}).get("contents", [])
                else:
                    log_message(f"Error fetching articles: {response.status}", "ERROR")
                    return []
        except Exception as e:
            log_message(f"Error in fetch_latest_articles: {e}", "ERROR")
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
            json.dump(creds, f)
    except Exception as e:
        log_message(f"Error saving credentials: {e}", "ERROR")


async def extract_ticker(url, session_data):
    """Extract ticker using browser session cookies"""
    try:
        async with create_session_with_cookies(session_data["cookies"]) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    content = await response.text()
                    ticker_match = re.search(
                        r"(NYSE|NASDAQ):\s*(\w+)", content, re.IGNORECASE
                    )
                    if ticker_match:
                        return ticker_match.group(2)
    except Exception as e:
        log_message(f"Error extracting ticker: {e}", "ERROR")
    return None


async def process_article(article, session_data):
    try:
        published_date = datetime.fromisoformat(
            article["publishAt"].replace("Z", "+00:00")
        )
        current_time = datetime.now(pytz.UTC)

        if (
            current_time - published_date
        ).total_seconds() < 86400:  # Within last 24 hours
            product_name = PRODUCT_NAMES.get(article["productId"], "Unknown Product")
            article_url = f"https://www.fool.com{article['path']}"

            ticker = await extract_ticker(article_url, session_data)

            message = (
                f"<b>New {product_name} Article!</b>\n"
                f"<b>Published Date:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>Title:</b> {article['headline']}\n"
                f"<b>URL:</b> {article_url}\n"
            )
            if ticker:
                message += f"<b>Ticker:</b> {ticker}\n"

            log_message(f"Sending new article alert: {article['headline']}", "INFO")

            await asyncio.gather(
                send_ws_message(
                    {
                        "name": product_name,
                        "type": "Buy",
                        "ticker": (
                            ticker
                            if ticker
                            else f"NO_TICKER_IGNORE - Title: {article['headline']}"
                        ),
                        "sender": "motley_fool",
                    },
                    WS_SERVER_URL,
                ),
                send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID),
            )
            return True
    except Exception as e:
        log_message(f"Error processing article: {e}", "ERROR")
    return False


async def check_for_new_alerts(prev_articles, session_data):
    try:
        start_time = time.time()
        current_articles = await fetch_latest_articles(session_data)

        duration = time.time() - start_time
        log_message(
            f"fetch_alert_details took {duration:.2f} seconds. Total Articles: {len(prev_articles)}",
            "ERROR",
        )
        if not current_articles:
            return prev_articles, []

        new_articles = []
        known_urls = load_processed_urls()

        for article in current_articles:
            article_url = article["path"]
            if article_url not in known_urls:
                new_articles.append(article)
                known_urls.add(article_url)

        if new_articles:
            log_message(f"Found {len(new_articles)} new articles", "INFO")
            processing_tasks = [
                process_article(article, session_data) for article in new_articles
            ]
            await asyncio.gather(*processing_tasks)

            save_processed_urls(known_urls)

        return current_articles, new_articles

    except Exception as e:
        log_message(f"Error in check_for_new_alerts: {e}", "ERROR")
        return prev_articles, []


async def run_alert_monitor():
    prev_articles = []
    session_data = load_session_credentials()

    if not session_data:
        session_data = await get_new_session_token()
        if not session_data:
            log_message("Failed to get session token", "CRITICAL")
            return

    while True:
        try:
            await sleep_until_market_open()
            log_message("Market is open. Starting to check for new articles...")

            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = datetime.now(pytz.timezone("America/New_York"))
                if current_time > market_close_time:
                    log_message("Market is closed. Waiting for next market open...")
                    break

                if datetime.fromisoformat(
                    session_data["expires"].replace("Z", "+00:00")
                ) < datetime.now(pytz.UTC):
                    session_data = await get_new_session_token()
                    if not session_data:
                        raise Exception("Failed to refresh session token")

                prev_articles, _ = await check_for_new_alerts(
                    prev_articles, session_data
                )
                await asyncio.sleep(2)

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
        asyncio.run(run_alert_monitor())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
