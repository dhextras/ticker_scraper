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
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumrequests import Chrome

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("FOOL_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("FOOL_SCRAPER_TELEGRAM_GRP")
FOOL_USERNAME = os.getenv("FOOL_USERNAME")
FOOL_PASSWORD = os.getenv("FOOL_PASSWORD")
FOOL_API_KEY = os.getenv("FOOL_API_KEY")
PROCESSED_URLS_FILE = "data/motley_rec_processed_urls.json"
CREDS_PATH = "cred/fool_session.json"
HTML_OUTPUT_DIR = "data/recommendations"

os.makedirs("cred", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs(HTML_OUTPUT_DIR, exist_ok=True)

# Global variables
PRODUCT_MAPPING = {
    1081: {"id": "18", "path": "coverage/updates"},  # Stock Advisor
    1069: {"id": "1069", "path": "coverage"},  # Rule Breakers
    4198: {"id": "4198", "path": "coverage"},  # Hidden Gems
    4488: {"id": "4488", "path": "coverage"},  # Dividend Investor
}

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
                "expires": (get_current_time() + timedelta(days=1)).isoformat(),
            }
            save_session_credentials(session_data)
            return session_data

        return None

    except Exception as e:
        log_message(f"Error in login process: {e}", "ERROR")
        return None
    finally:
        driver.quit()


async def check_recommendation_url(session, product_id, date, session_data, known_urls):
    product_info = PRODUCT_MAPPING[product_id]
    base_url = f"https://www.fool.com/premium/{product_info['id']}/{product_info['path']}/{date.strftime('%Y/%m/%d')}/our-next-recommendation-is"

    if base_url in known_urls:
        return None, None

    cache_uuid = uuid4()
    timestamp = int(time.time() * 10000)
    cache_buster = get_random_cache_buster()
    full_url = f"{base_url}?{cache_buster}"
    cookies = {"__Secure-authjs.session-token": session_data["session_token"]}

    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "dnt": "1",
        "priority": "u=0, i",
        "sec-fetch-site": "same-origin",
        "cache-control": "no-cache, no-store, max-age=0, must-revalidate, private",
        "pragma": "no-cache",
        "cache-timestamp": str(timestamp),
        "cache-uuid": str(cache_uuid),
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    }

    try:
        async with session.get(full_url, headers=headers, cookies=cookies) as response:
            if response.status == 200:
                content = await response.text()
                soup = BeautifulSoup(content, "html.parser")

                # Check for "Article not found"
                not_found = soup.select_one(
                    "#dynamic-content > main > div > div > div.article-backdrop > div > div > h3"
                )
                if not_found and "Article not found" in not_found.text:
                    log_message(
                        f"Article not found for {PRODUCT_NAMES[int(product_id)]}",
                        "INFO",
                    )
                    return None, None

                title = soup.select_one("#article-section > h1")
                if not title:
                    log_message(
                        f"Article found but no title for {PRODUCT_NAMES[int(product_id)]}. url: {base_url}",
                        "WARNING",
                    )
                    return None, None

                title_text = title.text.strip()
                log_message(
                    f"Found valid recommendation for {PRODUCT_NAMES[int(product_id)]}: {title_text}",
                    "INFO",
                )

                timestamp = get_current_time().strftime("%Y%m%d_%H%M%S")
                filename = f"{HTML_OUTPUT_DIR}/recommendation_{product_info['id']}_{timestamp}.html"
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(content)
                return base_url, title_text

            elif response.status == 404:
                log_message(
                    f"No valid url found for {PRODUCT_NAMES[int(product_id)]}", "INFO"
                )
                return None, None
            log_message(
                f"Failed to fetch rec article for {PRODUCT_NAMES[int(product_id)]}. Status: {response.status}",
                "ERROR",
            )
            return None, None
    except Exception as e:
        log_message(f"Error checking URL {base_url}: {e}", "ERROR")
        return None, None


def load_session_credentials():
    try:
        if os.path.exists(CREDS_PATH):
            with open(CREDS_PATH, "r") as f:
                creds = json.load(f)
                if (
                    datetime.fromisoformat(creds["expires"].replace("Z", "+00:00"))
                    > get_current_time()
                ):
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


async def process_recommendation(url, title, product_id, known_urls):
    try:
        current_time = get_current_time()
        product_name = PRODUCT_NAMES.get(product_id, "Unknown")

        message = (
            f"<b>New {product_name} Recommendation found!</b>\n"
            f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"<b>Title:</b> {title}\n"
            f"<b>URL:</b> {url}\n"
        )
        log_message(f"Sending new recommendation: {url}", "INFO")

        await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        known_urls.add(url)
        save_processed_urls(known_urls)
        return True
    except Exception as e:
        log_message(f"Error processing recommendation: {e}", "ERROR")
    return False


async def check_for_new_recommendations(session_data):
    known_urls = load_processed_urls()
    current_date = get_current_time()

    async with aiohttp.ClientSession() as session:
        for product_id in PRODUCT_MAPPING.keys():
            await rate_limiter.acquire()

            url, title = await check_recommendation_url(
                session, product_id, current_date, session_data, known_urls
            )

            if url:
                await process_recommendation(url, title, product_id, known_urls)


async def run_recommendation_monitor():
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
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                if (
                    datetime.fromisoformat(
                        session_data["expires"].replace("Z", "+00:00")
                    )
                    < get_current_time()
                ):
                    session_data = await get_new_session_token()
                    if not session_data:
                        raise Exception("Failed to refresh session token")

                await check_for_new_recommendations(session_data)
                await asyncio.sleep(5)  # Remove later

        except Exception as e:
            log_message(f"Error in monitor loop: {e}", "ERROR")
            await asyncio.sleep(5)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, FOOL_USERNAME, FOOL_PASSWORD]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_recommendation_monitor())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
