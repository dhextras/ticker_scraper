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
        json.dump(list(urls), f, indent=2)
    log_message("Processed URLs saved.", "INFO")


def get_random_cache_buster():
    """Generate random cache busting url variable for requests"""
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
    return variable, value_generator()


async def get_api_session(driver):
    """Fetch the API session token for GraphQL requests"""
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


async def fetch_latest_articles(session_data):
    await rate_limiter.acquire()
    timestamp = int(time.time() * 10000)
    cache_uuid = uuid4()
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

    # FIXME: Later add the images here and see if it fast or not if needed only other wise just leave the other script be
    query = """
        query GetLatestUniqueArticles($limit: Int!, $productIds: [Int!]!) {
            contents(productIds: $productIds, limit: $limit) {
            product {
                productId
            }
            recommendations {
                action
                instrument {
                    name
                    symbol
                }
            }
            url
            path
            publishAt
            }
        }
    """

    params = {
        "operationName": "GetLatestUniqueArticles",
        "query": query,
        "variables": json.dumps(
            {"limit": 10, "productIds": list(PRODUCT_NAMES.keys())}
        ),
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                base_url, params=params, headers=headers
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
            json.dump(creds, f, indent=2)
    except Exception as e:
        log_message(f"Error saving credentials: {e}", "ERROR")


async def extract_tickers(article):
    """Extract ticker using browser session cookies"""
    try:
        tickers = []
        recs = article["recommendations"]

        for rec in recs:
            action = rec.get("action", None)
            symbol = rec.get("instrument", {}).get("symbol", None)
            name = rec.get("instrument", {}).get("name", "")
            if action and symbol:
                tickers.append((action.lower().capitalize(), symbol, name))

        return tickers
    except Exception as e:
        log_message(f"Error extracting tickers: {e}", "ERROR")
    return []


async def process_rec_article(article):
    try:
        published_date = datetime.fromisoformat(
            article["publishAt"].replace("Z", "+00:00")
        )
        current_time = datetime.now(pytz.UTC)

        if (
            current_time - published_date
        ).total_seconds() < 86400:  # Within last 24 hours
            product_name = PRODUCT_NAMES.get(article["product"]["productId"], "Unknown")
            article_url = article["url"]

            # FIXME: Remove this after confirming
            if product_name != "Unknown":
                date = datetime.now().strftime("%Y_%m_%d_%H_%M_%S_%f")
                with open(f"data/motley_remove_{date}.json", "w") as f:
                    json.dump(article, f)

            tickers = await extract_tickers(article)

            message = (
                f"<b>New {product_name} Article!</b>\n"
                f"<b>Published Date:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>URL:</b> {article_url}\n"
            )

            if tickers:
                ticker_text = "\n".join(
                    [
                        f"- {action}: {ticker}, company: {name}"
                        for action, ticker, name in tickers
                    ]
                )

                for (
                    action,
                    ticker,
                    _,
                ) in tickers:
                    await send_ws_message(
                        {
                            "name": product_name,
                            "type": action,
                            "ticker": ticker,
                            "sender": "motley_fool",
                            "target": "CSS",
                        },
                        WS_SERVER_URL,
                    )

                message += f"<b>Tickers:</b>\n{ticker_text}\n"

            log_message(f"Sending new article alert: {article['path']}", "INFO")

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
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
            "INFO",
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
                process_rec_article(article) for article in new_articles
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
            log_message(
                "Market is open. Starting to check for new articles...", "DEBUG"
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

                prev_articles, _ = await check_for_new_alerts(
                    prev_articles, session_data
                )
                await asyncio.sleep(1)  # Remove in the future if it becomes slow

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
