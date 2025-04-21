import asyncio
import json
import os
import random
import re
import sys
import uuid
from pathlib import Path
from time import sleep, time

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from DrissionPage import ChromiumOptions, ChromiumPage

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
TELEGRAM_BOT_TOKEN = os.getenv("ZACKS_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("ZACKS_TELEGRAM_GRP")
CHECK_INTERVAL = 0.5  # seconds
STARTING_CID = 43250  # Starting comment ID
BROWSER_REFRESH_INTERVAL = 1800  # Half an hour

DATA_DIR = Path("data")
CRED_DIR = Path("cred")
COMMENT_ID_FILE = DATA_DIR / "zacks_last_comment_id.json"
COOKIES_HEADERS_FILE = DATA_DIR / "zacks_session_data.json"
CREDENTIALS_FILE = CRED_DIR / "zacks_credentials.json"

# Initialize browser variables
co = None
page = None
session_cookies = {}
session_headers = {}
current_account_index = 0
accounts = []
total_accounts = 0


def load_credentials():
    """Load credentials from the JSON file"""
    global accounts, total_accounts

    try:
        if CREDENTIALS_FILE.exists():
            with open(CREDENTIALS_FILE, "r") as f:
                accounts = json.load(f)
                total_accounts = len(accounts)
                if total_accounts == 0:
                    log_message("No accounts found in credentials file", "CRITICAL")
                    sys.exit(1)
                log_message(
                    f"Loaded {total_accounts} accounts from credentials file", "INFO"
                )
                return True
        else:
            log_message(f"Credentials file not found at {CREDENTIALS_FILE}", "CRITICAL")
            sys.exit(1)
    except Exception as e:
        log_message(f"Error loading credentials: {e}", "CRITICAL")
        sys.exit(1)


def get_next_account():
    """Get the next account to use"""
    global current_account_index, accounts, total_accounts

    account = accounts[current_account_index]
    current_account_index = (current_account_index + 1) % total_accounts
    return account["email"], account["password"]


def initialize_browser():
    """Initialize a new browser instance and extract cookies/headers"""
    global co, page, session_cookies, session_headers

    email, password = get_next_account()
    log_message(f"Initializing new browser instance with account: {email}", "INFO")

    if page:
        try:
            page.quit()
            log_message("Successfully closed old browser instance", "INFO")
        except Exception as e:
            log_message(f"Error closing browser: {e}", "WARNING")

    co = ChromiumOptions()
    page = ChromiumPage(co)
    log_message("New browser instance created", "INFO")

    if login(email, password):
        extract_session_data()
    else:
        log_message(f"Failed to login with account: {email}", "ERROR")
        return False

    return True


def extract_session_data():
    """Extract cookies and headers from the browser session"""
    global session_cookies, session_headers

    try:
        log_message("Extracting session data for requests", "INFO")

        page.get("https://www.zacks.com/confidential")
        sleep(2)

        sample_cid = load_last_comment_id()
        page.get(f"https://www.zacks.com/confidential/commentary.php?cid={sample_cid}")
        sleep(2)

        browser_cookies = page.cookies()
        session_cookies = {
            cookie["name"]: cookie["value"] for cookie in browser_cookies
        }

        session_headers = {
            "User-Agent": page.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
            "Referer": "https://www.zacks.com/confidential",
            "Upgrade-Insecure-Requests": "1",
        }

        DATA_DIR.mkdir(exist_ok=True)
        with open(COOKIES_HEADERS_FILE, "w") as f:
            json.dump({"cookies": session_cookies, "headers": session_headers}, f)

        log_message("Successfully extracted and saved session data", "INFO")
        return True

    except Exception as e:
        log_message(f"Error extracting session data: {e}", "ERROR")
        return False


def load_session_data():
    """Load saved session data if available"""
    global session_cookies, session_headers

    try:
        if COOKIES_HEADERS_FILE.exists():
            with open(COOKIES_HEADERS_FILE, "r") as f:
                data = json.load(f)
                session_cookies = data.get("cookies", {})
                session_headers = data.get("headers", {})
                return True
        return False
    except Exception as e:
        log_message(f"Error loading session data: {e}", "ERROR")
        return False


def login(email, password):
    """Login to Zacks using DrissionPage"""
    try:
        log_message(f"Trying to login with account: {email}", "INFO")
        page.get("https://www.zacks.com/my-account/")
        sleep(2)

        if is_logged_in():
            log_message("Already logged in, logging out first", "WARNING")
            try:
                logout_ele = page.ele("#logout", timeout=2)
                logout_ele.click()
                sleep(2)
            except:
                log_message("Failed to logout, clearing cookies", "WARNING")
                page.clear_cache()
                page.get("https://www.zacks.com/my-account/")
                sleep(2)

        page.get("https://www.zacks.com/my-account/")
        sleep(2)

        username_input = page.ele("#username_default")
        password_input = page.ele("#password_default")
        login_div = (
            page.ele("#ecommerce-login", timeout=0.1)
            .ele("tag:tbody")
            .eles("tag:tr", timeout=0.1)[4]
        )
        if not login_div:
            log_message("Cannot find login button", "ERROR")
            return False

        login_input = login_div.ele("tag:input", timeout=0.1)

        username_input.input(email)
        password_input.input(password)

        login_input.click()

        sleep(3)

        try:
            if is_logged_in():
                log_message(f"Login successful with account: {email}", "INFO")
                page.get("https://www.zacks.com/confidential")
                sleep(2)
                return True
        except:
            log_message(f"Login failed with account: {email}", "ERROR")
            return False

    except Exception as e:
        log_message(f"Error during login with account {email}: {e}", "ERROR")
        return False


def extract_ticker(title, content):
    if title == "We're Buying and Selling Today":
        buy_section = re.search(r"(Buy .*? Today)", content)
        if buy_section:
            match = re.search(r"\(([A-Z]+)\)", content[buy_section.start() :])
            if match:
                return match.group(1), "Buy"
    elif "BUY" in title or "Buy" in title or "Buying" in title:
        if "sell" in title.lower():
            match = re.search("buy", content.lower())
            match2 = re.search("hold", content.lower())
            if match:
                content = content[match.end() :]
            elif match2:
                content = content[match2.end() :]
        match = re.search(r"\(([A-Z]+)\)", content)
        if match:
            return match.group(1), "Buy"
    elif "Adding" in title:
        match = re.search(r"Adding\s+([A-Z]+)", title)
        if match:
            return match.group(1), "Buy"
    # TODO: Later also process sell alerts

    return None, None


def load_last_comment_id():
    """Load the last processed comment ID from file"""
    try:
        if COMMENT_ID_FILE.exists():
            with open(COMMENT_ID_FILE, "r") as f:
                data = json.load(f)
                return data.get("last_comment_id", STARTING_CID)
        return STARTING_CID
    except Exception as e:
        log_message(f"Error loading last comment ID: {e}", "ERROR")
        return STARTING_CID


async def save_comment_id(comment_id: int):
    """Save the last processed comment ID"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(COMMENT_ID_FILE, "w") as f:
            json.dump({"last_comment_id": comment_id}, f)
    except Exception as e:
        log_message(f"Error saving comment ID: {e}", "ERROR")


def is_logged_in():
    """Check if we are still logged in"""
    try:
        logout_ele = page.ele("#logout", timeout=5)
        if "NoneElement" in str(logout_ele):
            return False
        return True
    except:
        return False


def get_random_cache_buster():
    cache_busters = [
        ("cache_timestamp", lambda: int(time() * 10000)),
        ("request_uuid", lambda: str(uuid.uuid4())),
        ("cache_time", lambda: int(time())),
        ("ran_time", lambda: int(time() * 1000)),
        ("no_cache_uuid", lambda: str(uuid.uuid4().hex[:16])),
        ("unique", lambda: f"{int(time())}-{random.randint(1000, 9999)}"),
        ("req_uuid", lambda: f"req-{uuid.uuid4().hex[:8]}"),
        ("tist", lambda: str(int(time()))),
    ]

    variable, value_generator = random.choice(cache_busters)
    return variable, value_generator()


def fetch_commentary_with_requests(comment_id: int):
    """Fetch commentary using requests library instead of browser"""
    global session_cookies, session_headers

    try:
        key, value = get_random_cache_buster()
        url = f"https://www.zacks.com/confidential/commentary.php?cid={comment_id}&{key}={value}"

        response = requests.get(
            url, cookies=session_cookies, headers=session_headers, timeout=10
        )

        if response.status_code == 200 and "About Zacks Confidential" in response.text:
            return response.text
        else:
            log_message(
                f"Request failed or content not as expected: Status {response.status_code}",
                "WARNING",
            )
            return None

    except Exception as e:
        log_message(f"Error fetching commentary with requests: {e}", "ERROR")
        return None


def process_commentary(html: str):
    """Extract title and content from commentary HTML"""
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Find the title and content using the new selectors
        title_elem = soup.select_one("#cdate-most-recent > article > div > h2")
        content_elem = soup.select_one("#cdate-most-recent > article > div")

        if not title_elem or not content_elem:
            return None

        title = title_elem.get_text(strip=True)
        content = content_elem.get_text(strip=True)

        if title in content:
            content = content.replace(title, "", 1)

        if not title or not content:
            return None

        ticker, action = extract_ticker(title, content)

        return {"title": title, "content": content, "ticker": ticker, "action": action}
    except Exception as e:
        log_message(f"Error processing commentary: {e}", "ERROR")
        return None


async def try_with_another_account():
    """Try with another account when the current one fails"""
    global current_account_index, accounts

    # If we've tried all accounts and still failed, exit
    accounts_tried = 0

    while accounts_tried < total_accounts:
        accounts_tried += 1
        email, password = get_next_account()
        log_message(f"Switching to account: {email}", "INFO")

        # Clear cookies and cache
        if page:
            try:
                page.clear_cache()
                log_message("Browser cache cleared", "INFO")
            except Exception as e:
                log_message(f"Error clearing cache: {e}", "WARNING")
                # Try to recreate the browser
                try:
                    page.quit()
                except:
                    pass
                co = ChromiumOptions()
                page = ChromiumPage(co)

        # Try to login with the new account
        if login(email, password):
            if extract_session_data():
                log_message(f"Successfully switched to account: {email}", "INFO")
                return True

        log_message(f"Failed to login with account: {email}", "ERROR")

    log_message("All accounts have failed. Exiting.", "CRITICAL")
    return False


async def run_scraper():
    """Main scraper loop that respects market hours"""
    try:
        load_credentials()

        if not initialize_browser():
            if not await try_with_another_account():
                log_message(
                    "Failed to initialize with any account. Exiting.", "CRITICAL"
                )
                return

        if not session_cookies or not session_headers:
            log_message("Failed to initialize session data. Exiting.", "CRITICAL")
            return

        current_comment_id = load_last_comment_id()
        last_browser_refresh_time = time()
        consecutive_failures = 0

        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message("Market is open. Starting commentary monitoring...", "DEBUG")

            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                current_timestamp = time()
                if (
                    current_timestamp - last_browser_refresh_time
                    >= BROWSER_REFRESH_INTERVAL
                ):
                    log_message(
                        "Time to refresh browser instance and session data", "INFO"
                    )
                    if not initialize_browser():
                        if not await try_with_another_account():
                            log_message(
                                "All accounts have failed. Exiting.", "CRITICAL"
                            )
                            return
                    last_browser_refresh_time = current_timestamp

                start_time = time()
                log_message(f"Checking comment ID: {current_comment_id}")

                html = fetch_commentary_with_requests(current_comment_id)

                if not html:
                    log_message(
                        "Requests fetch failed, trying with browser as fallback",
                        "WARNING",
                    )
                    if not is_logged_in():
                        email, password = (
                            accounts[current_account_index - 1]["email"],
                            accounts[current_account_index - 1]["password"],
                        )
                        if not login(email, password):
                            log_message(
                                "Browser login failed too, trying with another account",
                                "ERROR",
                            )
                            if not await try_with_another_account():
                                log_message(
                                    "All accounts have failed. Exiting.", "CRITICAL"
                                )
                                return

                    page.get(
                        f"https://www.zacks.com/confidential/commentary.php?cid={current_comment_id}"
                    )

                    extract_session_data()

                    html = fetch_commentary_with_requests(current_comment_id)

                    if not html:
                        log_message(
                            "Both request methods failed, skipping this comment",
                            "ERROR",
                        )
                        consecutive_failures += 1

                        if consecutive_failures >= 3:
                            log_message(
                                "Multiple consecutive failures, trying with another account",
                                "WARNING",
                            )
                            if not await try_with_another_account():
                                log_message(
                                    "All accounts have failed. Exiting.", "CRITICAL"
                                )
                                return
                            consecutive_failures = 0

                        await asyncio.sleep(CHECK_INTERVAL)
                        continue

                consecutive_failures = 0

                fetched_time = get_current_time()
                commentary = process_commentary(html)
                if commentary:
                    log_message(
                        f"Found comment: {current_comment_id}, Title: {commentary['title']}",
                        "INFO",
                    )

                    ticker_info = ""
                    if commentary["ticker"] and commentary["action"]:
                        ticker_info = f"\n<b>Action:</b> {commentary['action']} {commentary['ticker']}"

                        await send_ws_message(
                            {
                                "name": "Zacks - Commentary",
                                "type": commentary["action"],
                                "ticker": commentary["ticker"],
                                "sender": "zacks",
                                "target": "CSS",
                            },
                        )

                    message = (
                        f"<b>New Zacks Commentary!</b>\n"
                        f"<b>Current Time:</b> {fetched_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                        f"<b>Comment Id:</b> {current_comment_id}{ticker_info}\n\n"
                        f"<b>Title:</b> {commentary['title']}\n\n"
                        f"{commentary['content'][:600]}\n\n\nthere is more......."
                    )

                    await send_telegram_message(
                        message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                    )

                    current_comment_id += 1
                    await save_comment_id(current_comment_id)

                total_time = time() - start_time
                log_message(f"Checking comment completed in {total_time:.2f} seconds")
                await asyncio.sleep(CHECK_INTERVAL)
    except Exception as e:
        log_message(f"Critical error in run_scraper: {e}", "CRITICAL")


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        if not load_session_data():
            log_message(
                "No saved session data found, will create after browser init", "INFO"
            )

        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        if page:
            page.quit()
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        if page:
            page.quit()
        sys.exit(1)


if __name__ == "__main__":
    main()
