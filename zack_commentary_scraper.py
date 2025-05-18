import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from time import sleep, time

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
CHECK_INTERVAL = 0.6  # seconds
STARTING_CID = 43250  # Starting comment ID
BROWSER_REFRESH_INTERVAL = 1800  # Half an hour
ACCOUNT_COOL_DOWN_DEFAULT = 15 * 60  # Default cool down period in seconds (15 minutes)
NUM_TABS = 5  # Number of tabs to use

DATA_DIR = Path("data")
CRED_DIR = Path("cred")
COMMENT_ID_FILE = DATA_DIR / "zacks_last_comment_id.json"
COOKIES_HEADERS_FILE = DATA_DIR / "zacks_session_data.json"
CREDENTIALS_FILE = CRED_DIR / "zacks_credentials.json"
ACCOUNT_STATUS_FILE = DATA_DIR / "zacks_account_status.json"

# Initialize browser variables
co = None
page = None
session_cookies = {}
session_headers = {}
current_account_index = 0
accounts = []
total_accounts = 0
account_status = {}  # To track banned accounts and their cool-down periods
tab_ids = []  # Store tab IDs


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


def load_account_status():
    """Load account status from file"""
    global account_status

    try:
        if ACCOUNT_STATUS_FILE.exists():
            with open(ACCOUNT_STATUS_FILE, "r") as f:
                account_status = json.load(f)

                current_time = datetime.now().timestamp()
                for email in list(account_status.keys()):
                    if account_status[email]["banned_until"] <= current_time:
                        account_status[email]["banned"] = False
                        account_status[email]["banned_until"] = 0

                banned_accounts = [
                    email
                    for email, status in account_status.items()
                    if status["banned"]
                ]
                if banned_accounts:
                    log_message(
                        f"Currently banned accounts: {', '.join(banned_accounts)}",
                        "INFO",
                    )

                return True
        else:
            for account in accounts:
                account_status[account["email"]] = {
                    "banned": False,
                    "banned_until": 0,
                    "ban_count": 0,
                }
            save_account_status()
    except Exception as e:
        log_message(f"Error loading account status: {e}", "ERROR")
        for account in accounts:
            account_status[account["email"]] = {
                "banned": False,
                "banned_until": 0,
                "ban_count": 0,
            }
        save_account_status()

    return True


def save_account_status():
    """Save account status to file"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(ACCOUNT_STATUS_FILE, "w") as f:
            json.dump(account_status, f)
        return True
    except Exception as e:
        log_message(f"Error saving account status: {e}", "ERROR")
        return False


def get_next_account():
    """Get the next available non-banned account to use"""
    global current_account_index, accounts, total_accounts, account_status

    # Check if all accounts are banned
    current_time = datetime.now().timestamp()
    all_banned = True

    for i in range(total_accounts):
        idx = (current_account_index + i) % total_accounts
        email = accounts[idx]["email"]

        if email not in account_status:
            account_status[email] = {"banned": False, "banned_until": 0, "ban_count": 0}

        if (
            not account_status[email]["banned"]
            or current_time > account_status[email]["banned_until"]
        ):
            if account_status[email]["banned"]:
                account_status[email]["banned"] = False
                account_status[email]["banned_until"] = 0
                log_message(
                    f"Account {email} ban period has expired, now available", "INFO"
                )
                save_account_status()

            current_account_index = idx
            all_banned = False
            account = accounts[current_account_index]
            current_account_index = (current_account_index + 1) % total_accounts
            return account["email"], account["password"], False

    if all_banned:
        earliest_expiry = float("inf")
        earliest_idx = 0

        for i in range(total_accounts):
            email = accounts[i]["email"]
            if account_status[email]["banned_until"] < earliest_expiry:
                earliest_expiry = account_status[email]["banned_until"]
                earliest_idx = i

        wait_time = max(0, earliest_expiry - current_time)
        log_message(
            f"All accounts are banned. Earliest available in {wait_time:.1f} seconds",
            "ERROR",
        )

        # Return the account with the earliest expiration
        current_account_index = earliest_idx
        account = accounts[current_account_index]
        current_account_index = (current_account_index + 1) % total_accounts
        return account["email"], account["password"], True


def ban_account(email, minutes=None):
    """Mark an account as banned and set the cool-down period"""
    global account_status

    if email not in account_status:
        account_status[email] = {"banned": False, "banned_until": 0, "ban_count": 0}

    cool_down_seconds = ACCOUNT_COOL_DOWN_DEFAULT
    if minutes:
        cool_down_seconds = minutes * 60

    current_time = datetime.now().timestamp()
    banned_until = current_time + cool_down_seconds

    account_status[email]["banned"] = True
    account_status[email]["banned_until"] = banned_until
    account_status[email]["ban_count"] += 1

    ban_expiry_time = datetime.fromtimestamp(banned_until).strftime("%Y-%m-%d %H:%M:%S")
    log_message(
        f"Account {email} banned until {ban_expiry_time} ({cool_down_seconds/60:.1f} minutes)",
        "ERROR",
    )

    save_account_status()
    return True


def simulate_human_browser_behavior(max_pages=3, sleep_interval=10):
    """
    Simulate human-like browsing behavior

    Args:
        max_pages (int): Maximum number of pages to visit (default: 4)
        sleep_interval (int): Maximum sleep time in seconds (default: 10)
                              Sleep times will be random between 0 and this value
    """
    try:
        log_message(
            f"Simulating human browsing behavior with max_pages={max_pages}, sleep_interval={sleep_interval}...",
            "INFO",
        )

        tickers = [
            "AAPL",
            "MSFT",
            "AMZN",
            "TSLA",
            "GOOG",
            "GOOGL",
            "META",
            "NVDA",
            "BRK.B",
            "JPM",
            "V",
            "JNJ",
            "WMT",
            "PG",
            "MA",
            "UNH",
            "HD",
            "BAC",
            "XOM",
            "AVGO",
            "LLY",
            "COST",
            "PFE",
            "CSCO",
            "TMO",
            "MRK",
            "ABT",
            "PEP",
            "CVX",
            "KO",
            "ADBE",
            "NKE",
            "CRM",
            "CMCSA",
            "NFLX",
            "AMD",
            "VZ",
            "INTC",
            "DIS",
            "QCOM",
            "T",
            "IBM",
            "TXN",
            "PYPL",
            "MCD",
            "TMUS",
            "AMAT",
            "GS",
            "BLK",
            "MS",
        ]

        # Base URLs with placeholders for tickers
        base_pages = [
            "https://www.zacks.com/",
            "https://www.zacks.com/stocks/",
            "https://www.zacks.com/earnings/",
            "https://www.zacks.com/stock/quote/{ticker}?q={ticker}",
            "https://www.zacks.com/stock/research/equity-research.php?icid=quote-temp_overview-zp_internal-zacks_premium-research_reports-all_reports",
            "https://www.zacks.com/research-daily/2426383/top-analyst-reports-for-unitedhealth-sap-toyota-motor?q={ticker}",
            "https://www.zacks.com/stock/quote/{ticker}/dashboard?art_rec=quote-temp_overview-dashboard_preview-zcom-preview_bar-stock_dashboard_{ticker}",
            "https://www.zacks.com/stocksunder10/",
            "https://www.zacks.com/homerun/?adid=TOP_ONLINE_NAV",
        ]

        # Generate actual pages to visit by replacing {ticker} with random tickers
        common_pages = []
        for base_url in base_pages:
            if "{ticker}" in base_url:
                ticker = random.choice(tickers)
                page_url = base_url.format(ticker=ticker)
            else:
                page_url = base_url
            common_pages.append(page_url)

        pages_to_visit = random.sample(common_pages, min(max_pages, len(common_pages)))

        for page_url in pages_to_visit:
            log_message(f"Visiting page: {page_url}", "INFO")
            page.get(page_url)

            # Random scrolling behavior
            scroll_count = random.randint(3, 8)
            for _ in range(scroll_count):
                scroll_amount = random.randint(100, 500)
                page.scroll.down(scroll_amount)

                scroll_pause = random.uniform(0.5, min(2.0, sleep_interval / 5))
                sleep(scroll_pause)

            between_pages_sleep = random.uniform(1, sleep_interval)
            log_message(
                f"Sleeping for {between_pages_sleep:.2f} seconds between pages", "INFO"
            )
            sleep(between_pages_sleep)

        log_message("Human browsing simulation complete", "INFO")
    except Exception as e:
        log_message(f"Error during human simulation: {e}", "WARNING")


def initialize_browser():
    """Initialize a new browser instance and extract cookies/headers"""
    global co, page, session_cookies, session_headers

    email, password, is_banned = get_next_account()

    if is_banned:
        log_message(
            f"Using account {email} even though it's banned as all accounts are unavailable",
            "WARNING",
        )
    else:
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

    # Simulate human behavior before login
    simulate_human_browser_behavior()

    # Now login
    if login(email, password):
        extract_session_data()
        return True
    else:
        log_message(f"Failed to login with account: {email}", "ERROR")
        return False


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


def check_for_access_denied():
    """Check if the page shows an access denied message"""
    try:
        # Look for access denied text
        access_denied_text = page.ele("text:Access Denied", timeout=1)

        if access_denied_text:
            # Try to find the timeout minutes
            timeout_text = page.ele(
                "text:will be restored in approximately:", timeout=1
            )
            minutes = ACCOUNT_COOL_DOWN_DEFAULT // 60  # Default 15 minutes

            if timeout_text:
                # Try to extract the actual minutes
                minutes_match = re.search(r"(\d+)\s*minutes", page.html)
                if minutes_match:
                    minutes = int(minutes_match.group(1))

            # Get current account email
            current_idx = (current_account_index - 1) % total_accounts
            if current_idx < 0:
                current_idx = total_accounts - 1

            email = accounts[current_idx]["email"]

            # Ban the account
            ban_account(email, minutes)
            return True, minutes

        return False, 0

    except Exception as e:
        log_message(f"Error checking for access denied: {e}", "WARNING")
        return False, 0


def login(email, password):
    """Login to Zacks using DrissionPage"""
    try:
        log_message(f"Trying to login with account: {email}", "INFO")
        page.get("https://www.zacks.com/my-account/")
        sleep(2)

        # Check if we're already redirected to an access denied page
        denied, minutes = check_for_access_denied()
        if denied:
            log_message(
                f"Account {email} is already banned. Access denied before login.",
                "ERROR",
            )
            return False

        if is_logged_in():
            log_message("Already logged in, logging out first", "WARNING")
            try:
                logout_ele = page.ele("#logout", timeout=2)
                logout_ele.click()
                sleep(4)
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

        username_input.clear()
        password_input.clear()
        username_input.input(email)
        password_input.input(password)

        login_input.click()

        sleep(3)

        # Check if we got an access denied page after login
        denied, _ = check_for_access_denied()
        if denied:
            log_message(f"Account {email} banned after login attempt", "ERROR")
            return False

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


def fetch_commentary_with_browser(tab, comment_id: int):
    """Fetch commentary using the browser with efficient content loading check"""
    try:
        timeout = 5  # seconds to wait for content
        cache_buster = f"t={int(time() * 1000)}"
        url = f"https://www.zacks.com/confidential/commentary.php?cid={comment_id}&{cache_buster}"

        start_time = time()
        tab.get(url)

        while time() - start_time < timeout:
            try:
                # Check if the target element exists
                content_elem = tab.ele("#cdate-most-recent", timeout=0.1)
                if content_elem:
                    log_message(
                        f"Content loaded for comment ID {comment_id} in {time() - start_time:.2f} seconds"
                    )
                    return tab.html
            except:
                pass

            sleep(0.1)

        # Check for access denied
        denied, _ = check_for_access_denied()
        if denied:
            log_message("Access denied when fetching commentary", "ERROR")
            return None

        # FIX: Move this into a warning later
        log_message(
            f"Timeout waiting for content to load for comment ID {comment_id}",
            "ERROR",
        )
        return None

    except Exception as e:
        log_message(f"Error fetching commentary with browser: {e}", "ERROR")
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


def initialize_tabs():
    """Initialize multiple tabs for parallel processing"""
    global page, tab_ids

    # Clear existing tabs
    if tab_ids:
        for tab_id in tab_ids[1:]:  # Skip the first tab
            try:
                page.get_tab(tab_id).close()
            except Exception as e:
                log_message(f"Error closing tab: {e}", "WARNING")

    # Create additional tabs
    for _ in range(1, NUM_TABS):
        page.new_tab()
        page.get("https://www.zacks.com")

    tab_ids = page.tab_ids
    log_message(f"Initialized {len(tab_ids)} tabs", "INFO")

    # Return to the first tab
    page.activate_tab(tab_ids[0])

    return tab_ids


async def try_with_another_account():
    """Try with another account when the current one fails"""
    global co, page, current_account_index, accounts, tab_ids

    # If we've tried all accounts and still failed, exit
    accounts_tried = 0

    while accounts_tried < total_accounts:
        accounts_tried += 1
        email, password, is_banned = get_next_account()

        if is_banned:
            log_message(
                f"Account {email} is banned, but using it anyway as all accounts are unavailable",
                "WARNING",
            )
        else:
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
                tab_ids = []

        # Simulate human behavior before login
        simulate_human_browser_behavior()

        # Try to login with the new account
        if login(email, password):
            if extract_session_data():
                log_message(f"Successfully switched to account: {email}", "INFO")
                return True

        log_message(f"Failed to login with account: {email}", "ERROR")

    log_message(
        "All accounts have failed. Waiting 5 minutes before trying again.", "CRITICAL"
    )
    await asyncio.sleep(300)  # Sleep for 5 minutes
    return False


async def check_any_accounts_available():
    """Check if any accounts are available, if not sleep and wait"""
    current_time = datetime.now().timestamp()
    all_banned = True
    earliest_expiry = float("inf")

    for account in accounts:
        email = account["email"]
        if email in account_status:
            if (
                not account_status[email]["banned"]
                or current_time > account_status[email]["banned_until"]
            ):
                all_banned = False
                break
            elif account_status[email]["banned_until"] < earliest_expiry:
                earliest_expiry = account_status[email]["banned_until"]

    if all_banned and earliest_expiry != float("inf"):
        wait_time = max(0, earliest_expiry - current_time)
        next_available = datetime.fromtimestamp(earliest_expiry).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        log_message(
            f"All accounts are banned. Waiting until {next_available} ({wait_time:.1f} seconds)",
            "WARNING",
        )
        await asyncio.sleep(
            min(wait_time, 300)
        )  # Wait for the ban to expire or max 5 minutes
        return False

    return not all_banned


async def run_scraper():
    """Main scraper loop that respects market hours"""
    try:
        load_credentials()
        load_account_status()

        if not initialize_browser():
            if not await try_with_another_account():
                log_message(
                    "Failed to initialize with any account. Trying again later.",
                    "CRITICAL",
                )
                await asyncio.sleep(300)  # Sleep for 5 minutes before trying again

        current_comment_id = load_last_comment_id()
        last_browser_refresh_time = time()

        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message("Market is open. Starting commentary monitoring...", "DEBUG")

            _, _, market_close_time = get_next_market_times()

            tab_ids = initialize_tabs()

            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                # Check if any accounts are available
                accounts_available = await check_any_accounts_available()
                if not accounts_available:
                    log_message(
                        "No accounts available right now. Waiting...", "WARNING"
                    )
                    await asyncio.sleep(60)  # Wait a minute before checking again
                    continue

                current_timestamp = time()
                if (
                    current_timestamp - last_browser_refresh_time
                    >= BROWSER_REFRESH_INTERVAL
                ):
                    log_message(
                        "Time to refresh browser instance and session data", "INFO"
                    )
                    # Close all tabs and reinitialize
                    if page:
                        page.quit()

                    if not initialize_browser():
                        if not await try_with_another_account():
                            log_message(
                                "All accounts have failed. Waiting before retrying.",
                                "CRITICAL",
                            )
                            await asyncio.sleep(300)  # Sleep for 5 minutes
                            continue

                    if (
                        random.randint(1, 500) == 1
                    ):  # 1/400 chance of execution. So like 3 to 4 times every 30 min
                        simulate_human_browser_behavior(
                            max_pages=1,
                            sleep_interval=1,
                        )

                    tab_ids = initialize_tabs()

                    last_browser_refresh_time = current_timestamp

                for tab_idx, tab_id in enumerate(tab_ids):
                    log_message(
                        f"Processing tab {tab_idx+1} with comment ID: {current_comment_id}"
                    )

                    # Switch to this tab
                    page.activate_tab(tab_id)
                    current_tab = page.get_tab(tab_id)

                    html = fetch_commentary_with_browser(
                        current_tab, current_comment_id
                    )
                    if not html:
                        log_message(
                            f"Failed to fetch commentary for comment ID {current_comment_id}",
                            "WARNING",
                        )

                        # Check if we need to switch accounts
                        denied, _ = check_for_access_denied()
                        if denied or not is_logged_in():
                            log_message(
                                "Access denied or logged out, switching accounts",
                                "WARNING",
                            )
                            if not await try_with_another_account():
                                log_message(
                                    "All accounts failed, waiting before retry",
                                    "CRITICAL",
                                )
                                await asyncio.sleep(300)
                                break

                            # Reinitialize tabs after account switch
                            tab_ids = initialize_tabs()
                            break

                        # Continue to next tab
                        continue

                    # Process the commentary
                    commentary = process_commentary(html)
                    if commentary:
                        fetched_time = get_current_time()
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

                        log_message(
                            f"Found comment: {current_comment_id}, Title: {commentary['title']}",
                            "INFO",
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

        # Create necessary directories
        DATA_DIR.mkdir(exist_ok=True)
        CRED_DIR.mkdir(exist_ok=True)

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
