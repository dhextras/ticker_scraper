import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from time import sleep

import websockets
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from DrissionPage import ChromiumOptions, ChromiumPage

from utils.logger import log_message

load_dotenv()

# Constants
WEBSOCKET_URL = os.getenv("ZACKS_WEBSOCKET_URL")
HUMAN_SIMULATION_PROBABILITY = (
    0.1  # 10% chance of simulating human behavior between jobs
)
MAX_CONSECUTIVE_FAILURES = (
    3  # Maximum number of consecutive failures before restarting browser
)
RECONNECT_DELAY = 3  # Seconds to wait before reconnecting to server
BROWSER_RESTART_INTERVAL = 1800  # Restart browser every 30 minutes

# Initialize browser variables
co = None
page = None
consecutive_failures = 0
last_browser_restart = time.time()
current_login_email = None
current_login_password = None


def setup_browser():
    """Initialize a new browser instance"""
    global co, page, consecutive_failures, current_login_email, current_login_password

    if page:
        try:
            page.quit()
        except Exception as e:
            log_message(f"Error closing browser: {e}", "ERROR")

    co = ChromiumOptions()
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-infobars")
    co.set_argument("--disable-notifications")
    co.set_argument("--disable-popup-blocking")

    page = ChromiumPage(co)
    consecutive_failures = 0
    current_login_email = None
    current_login_password = None

    log_message(
        f"New browser instance created at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "INFO",
    )
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


def login(email, password):
    """Login to Zacks using DrissionPage"""
    global current_login_email, current_login_password

    try:
        log_message(f"Logging in with account: {email}", "INFO")
        page.get("https://www.zacks.com/my-account/")
        time.sleep(2)

        denied, _ = check_for_access_denied()
        if denied:
            log_message(
                f"Account {email} is already banned. Access denied before login.",
                "ERROR",
            )
            return False

        if is_logged_in():
            log_message("Already logged in, logging out first", "INFO")
            try:
                logout_ele = page.ele("#logout", timeout=2)
                logout_ele.click()
                time.sleep(3)
            except:
                log_message("Failed to logout, clearing cookies", "WARNING")
                page.clear_cache()
                page.get("https://www.zacks.com/my-account/")
                time.sleep(2)

        page.get("https://www.zacks.com/my-account/")
        time.sleep(2)

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

        time.sleep(3)

        denied, _ = check_for_access_denied()
        if denied:
            log_message(f"Account {email} banned after login attempt", "ERROR")
            return False

        try:
            if is_logged_in():
                log_message(f"Login successful with account: {email}", "INFO")
                current_login_email = email
                current_login_password = password
                page.get("https://www.zacks.com/confidential")
                time.sleep(2)
                return True
        except:
            log_message(f"Login failed with account: {email}", "ERROR")
            return False

    except Exception as e:
        log_message(f"Error during login with account {email}: {e}", "ERROR")
        return False


def check_for_access_denied():
    """Check if the page shows an access denied message"""
    try:
        access_denied_text = page.ele("text:Access Denied", timeout=1)

        if access_denied_text:
            timeout_text = page.ele(
                "text:will be restored in approximately:", timeout=1
            )
            minutes = 15  # Default 15 minutes

            if timeout_text:
                minutes_match = re.search(r"(\d+)\s*minutes", page.html)
                if minutes_match:
                    minutes = int(minutes_match.group(1))

            return True, minutes

        return False, 0

    except Exception as e:
        log_message(f"Error checking for access denied: {e}", "ERROR")
        return False, 0


def is_logged_in():
    """Check if we are still logged in"""
    try:
        logout_ele = page.ele("#logout", timeout=3)
        if "NoneElement" in str(logout_ele):
            return False
        return True
    except:
        return False


def fetch_commentary(comment_id):
    """Fetch commentary using the browser"""
    try:
        timeout = 6  # FIX: Increase this if we get banned
        cache_buster = f"t={int(time.time() * 1000)}"
        url = f"https://www.zacks.com/confidential/commentary.php?cid={comment_id}&{cache_buster}"

        start_time = time.time()
        page.get(url)

        while time.time() - start_time < timeout:
            try:
                # Check if the target element exists
                content_elem = page.ele("#cdate-most-recent", timeout=0.1)
                if content_elem:
                    log_message(
                        f"Content loaded for comment ID {comment_id} in {time.time() - start_time:.2f} seconds",
                        "INFO",
                    )
                    return True, page.html
            except:
                pass

            time.sleep(0.1)

        # Check for access denied
        denied, minutes = check_for_access_denied()
        if denied:
            log_message(
                f"Access denied when fetching commentary, banned for {minutes} minutes",
                "ERROR",
            )
            return False, None, minutes

        # FIX: Make this a warning later on
        log_message(
            f"Timeout waiting for content to load for comment ID {comment_id}",
            "ERROR",
        )
        return True, None

    except Exception as e:
        log_message(f"Error fetching commentary with browser: {e}", "ERROR")
        return True, None


def process_commentary(html):
    """Extract title and content from commentary HTML"""
    try:
        soup = BeautifulSoup(html, "html.parser")

        title_elem = soup.select_one("#cdate-most-recent > article > div > h2")
        content_elem = soup.select_one("#cdate-most-recent > article > div")

        if not title_elem or not content_elem:
            return None, None

        title = title_elem.get_text(strip=True)
        content = content_elem.get_text(strip=True)

        if title in content:
            content = content.replace(title, "", 1)

        if not title or not content:
            return None, None

        return title, content
    except Exception as e:
        log_message(f"Error processing commentary: {e}", "ERROR")
        return None, None


async def handle_job(websocket, job_data, processing_start_time):
    """Handle a job assignment from the server"""
    global consecutive_failures, current_login_email, current_login_password

    comment_id = job_data["comment_id"]
    account_index = job_data["account_index"]
    email = job_data["email"]
    password = job_data["password"]
    is_banned = job_data.get("is_banned", False)

    try:
        log_message(
            f"Received job for comment ID: {comment_id} with account {email}", "INFO"
        )

        if is_banned:
            log_message(
                f"Server indicates account {email} is likely banned, but trying anyway",
                "WARNING",
            )

        if not is_logged_in() or current_login_email != email:
            log_message(f"Need to login with account {email}", "INFO")
            if not login(email, password):
                await websocket.send(
                    json.dumps(
                        {
                            "type": "account_banned",
                            "account_index": account_index,
                            "minutes": 15,  # Default ban time
                        }
                    )
                )
                consecutive_failures += 1
                return

        success, html_content, *ban_minutes = await asyncio.to_thread(
            fetch_commentary, comment_id
        )

        if not success:
            await websocket.send(
                json.dumps(
                    {
                        "type": "account_banned",
                        "account_index": account_index,
                        "minutes": ban_minutes[0] if ban_minutes else 15,
                    }
                )
            )
            consecutive_failures += 1
            return

        if html_content:
            title, content = process_commentary(html_content)
            consecutive_failures = 0

            await websocket.send(
                json.dumps(
                    {
                        "type": "result",
                        "comment_id": comment_id,
                        "title": title,
                        "content": content,
                        "html_content": html_content is not None,
                        "processing_start_time": processing_start_time,
                    }
                )
            )
        else:
            consecutive_failures += 1

            await websocket.send(
                json.dumps(
                    {
                        "type": "result",
                        "comment_id": comment_id,
                        "html_content": False,
                        "processing_start_time": processing_start_time,
                    }
                )
            )

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            log_message(
                f"Too many consecutive failures ({consecutive_failures})", "ERROR"
            )

    except Exception as e:
        log_message(f"Error handling job: {e}", "ERROR")
        consecutive_failures += 1

        # Report error to server
        await websocket.send(
            json.dumps(
                {
                    "type": "result",
                    "comment_id": comment_id,
                    "error": str(e),
                    "html_content": False,
                    "processing_start_time": processing_start_time,
                }
            )
        )


async def main(CLIENT_ID):
    """Main client function"""
    global last_browser_restart, current_login_email, current_login_password, consecutive_failures

    if not setup_browser():
        log_message("Failed to set up browser, exiting", "CRITICAL")
        return

    reconnect_delay = RECONNECT_DELAY

    while True:
        try:
            log_message(f"Connecting to server at {WEBSOCKET_URL}", "INFO")
            async with websockets.connect(WEBSOCKET_URL) as websocket:
                await websocket.send(
                    json.dumps({"type": "register", "client_id": CLIENT_ID})
                )

                # Wait for registration acknowledgment
                response = await websocket.recv()
                data = json.loads(response)

                if data["type"] != "registration_ack":
                    log_message(f"Failed to register with server: {data}", "ERROR")
                    await asyncio.sleep(reconnect_delay)
                    continue

                log_message(f"Registered with server as client: {CLIENT_ID}", "INFO")
                reconnect_delay = RECONNECT_DELAY

                # Initial status update
                await websocket.send(
                    json.dumps({"type": "status_update", "status": "available"})
                )

                # Handle pings and messages
                last_pong_time = time.time()

                while True:
                    try:
                        # Use a timeout to receive messages so we can check for ping timeouts
                        message = await asyncio.wait_for(websocket.recv(), timeout=5)
                        data = json.loads(message)

                        if data["type"] == "ping":
                            await websocket.send(
                                json.dumps({"type": "pong", "client_id": CLIENT_ID})
                            )
                            last_pong_time = time.time()

                        elif data["type"] == "job":
                            processing_start_time = data.get(
                                "processing_start_time", time.time()
                            )
                            await handle_job(websocket, data, processing_start_time)

                            if random.random() < HUMAN_SIMULATION_PROBABILITY:
                                await asyncio.to_thread(simulate_human_browser_behavior)

                            await websocket.send(
                                json.dumps(
                                    {"type": "status_update", "status": "available"}
                                )
                            )

                        elif data["type"] == "initialize_login":
                            account_index = data["account_index"]
                            email = data["email"]
                            password = data["password"]

                            log_message(
                                f"Received initial login request for account {email}",
                                "INFO",
                            )
                            login_success = await asyncio.to_thread(
                                login, email, password
                            )

                            if login_success:
                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "login_result",
                                            "account_index": account_index,
                                            "success": True,
                                        }
                                    )
                                )
                            else:
                                # Check if banned and for how long
                                denied, minutes = await asyncio.to_thread(
                                    check_for_access_denied
                                )
                                ban_minutes = minutes if denied else 15

                                await websocket.send(
                                    json.dumps(
                                        {
                                            "type": "login_result",
                                            "account_index": account_index,
                                            "success": False,
                                            "minutes": ban_minutes,
                                        }
                                    )
                                )

                        elif data["type"] == "restart_browser":
                            log_message("Received request to restart browser", "INFO")
                            account_index = data["account_index"]
                            email = data["email"]
                            password = data["password"]

                            # Store credentials before restart
                            temp_email = email
                            temp_password = password

                            await asyncio.to_thread(setup_browser)

                            # Try to login after restart
                            login_success = await asyncio.to_thread(
                                login, temp_email, temp_password
                            )

                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "browser_restart_complete",
                                        "success": login_success,
                                        "account_index": account_index,
                                    }
                                )
                            )

                            # Reset consecutive failures after browser restart
                            consecutive_failures = 0

                    except asyncio.TimeoutError:
                        # Check if we've gone too long without a pong (30 seconds)
                        if time.time() - last_pong_time > 30:
                            log_message(
                                "No ping from server for 30 seconds, reconnecting",
                                "WARNING",
                            )
                            break
                        continue

        except websockets.exceptions.ConnectionClosed:
            log_message("Connection to server closed", "WARNING")
        except Exception as e:
            log_message(f"Error in main loop: {e}", "ERROR")

        log_message(f"Reconnecting in {reconnect_delay} seconds...", "INFO")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(
            reconnect_delay * 1.5, 30
        )  # Increase delay but cap at 30 seconds


if __name__ == "__main__":
    try:
        if not WEBSOCKET_URL:
            log_message("Couldn't found the websocket server url", "CRITICAL")
            sys.exit(1)
        if len(sys.argv) >= 1:
            try:
                argument = int(sys.argv[1])
            except Exception as e:
                log_message(f"Failed to get the client id: {e}", "CRITICAL")
                sys.exit(1)
        else:
            log_message("Add a client id 1 through 5", "CRITICAL")
            sys.exit(1)

        log_message("Starting Zacks client...", "INFO")
        asyncio.run(main(f"client-{argument}"))
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        if page:
            page.quit()
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        if page:
            page.quit()
        sys.exit(1)
