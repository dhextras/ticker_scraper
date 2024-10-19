import asyncio
import json
import os
import pickle
import random
import re
import time
from datetime import datetime

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requestium import Keys, Session
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumrequests import Chrome
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN = os.getenv("HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN")
HEDGEYE_SCRAPER_TELEGRAM_GRP = os.getenv("HEDGEYE_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

# Load accounts from credentials file
with open("cred/hedgeye_credentials.json", "r") as f:
    accounts = json.load(f)

options = Options()
options.add_argument(
    "--headless"
)  # Comment out if you running for the first time and trying to save the sessions
options.add_argument("--maximize-window")
options.add_argument("--disable-search-engine-choice-screen")
options.add_argument("--disable-extensions")
options.add_argument("--disable-popup-blocking")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_experimental_option("excludeSwitches", ["enable-automation"])

last_alert_details = {}

# User agent list
user_agents = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.59",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 OPR/78.0.4093.112",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/91.0.4472.80 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
]


def get_random_user_agent():
    return random.choice(user_agents)


def random_scroll(driver):
    """Perform random scrolling on the page."""
    for _ in range(random.randint(2, 5)):
        scroll_amount = random.randint(300, 600)
        driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
        time.sleep(random.uniform(1, 3))
        scroll_amount = random.randint(-300, -100)
        driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
        time.sleep(random.uniform(1, 3))
        driver.execute_script(f"window.scrollTo(0, 0);")


def login(driver, email, password):
    login_url = "https://accounts.hedgeye.com/users/sign_in"
    driver.get(login_url)

    try:
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.ID, "user_email"))
        )
    except TimeoutException:
        log_message(f"Timeout while loading login page for {email}", "ERROR")
        return False

    random_scroll(driver)

    try:
        email_input = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "user_email"))
        )
        email_input.send_keys(email)

        password_input = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "user_password"))
        )
        password_input.send_keys(password)
        password_input.send_keys(Keys.RETURN)

        WebDriverWait(driver, 60).until(EC.url_changes(login_url))

        if driver.current_url == login_url:
            retries = 30
            while retries > 0 and driver.current_url == login_url:
                log_message(
                    f"Login failed for {email}. Retrying with additional scrolling... Attempts left: {retries}",
                    "WARNING",
                )
                driver.get(login_url)
                WebDriverWait(driver, 60).until(
                    EC.presence_of_element_located((By.ID, "user_email"))
                )
                random_scroll(driver)

                email_input = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.ID, "user_email"))
                )
                email_input.clear()
                email_input.send_keys(email)

                password_input = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.ID, "user_password"))
                )
                password_input.clear()
                password_input.send_keys(password)
                password_input.send_keys(Keys.RETURN)

                try:
                    WebDriverWait(driver, 60).until(EC.url_changes(login_url))
                except TimeoutException:
                    retries -= 1
                    if retries == 0:
                        log_message(
                            f"Login failed for {email} after multiple attempts", "ERROR"
                        )
                        return False

        return True

    except Exception as e:
        log_message(f"Error during login for {email}: {str(e)}", "ERROR")
        return False


async def fetch_alert_details(session):
    async with aiohttp.ClientSession() as aio_session:
        async with aio_session.get(
            "https://app.hedgeye.com/feed_items/all",
            headers=session.headers,
            cookies=session.cookies,
        ) as response:
            html = await response.text()

    soup = BeautifulSoup(html, "html.parser")
    try:
        alert_title = soup.select_one(".article__header")
        if alert_title:
            alert_title = alert_title.get_text(strip=True)
        else:
            return None
    except Exception as e:
        log_message(f"Failed to fetch alert title: {e}", "ERROR")
        return None

    try:
        alert_price = soup.select_one(".currency.se-live-or-close-price")
        if alert_price:
            alert_price = alert_price.get_text(strip=True)
        else:
            return None
    except Exception as e:
        log_message(f"Failed to fetch alert price: {e}", "ERROR")
        return None

    try:
        created_at_utc = soup.select_one("time[datetime]")["datetime"]
    except Exception as e:
        log_message(f"Failed to fetch or parse created_at_utc: {e}", "ERROR")
        return None

    created_at = datetime.fromisoformat(created_at_utc.replace("Z", "+00:00"))
    edt = pytz.timezone("America/New_York")
    created_at_edt = created_at.astimezone(edt)
    current_time_edt = datetime.now(pytz.utc).astimezone(edt)

    return {
        "title": alert_title,
        "price": alert_price,
        "created_at": created_at_edt.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
        "current_time": current_time_edt.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
    }


def save_session(session, filename):
    with open(filename, "wb") as f:
        pickle.dump(session.cookies, f)


def load_session(filename):
    with open(filename, "rb") as f:
        return pickle.load(f)


async def monitor_feeds_async():
    global last_alert_details
    market_is_open = False
    logged_in = False
    first_time_ever = True
    sessions = []

    async def check_session(session, offset):
        global last_alert_details
        await asyncio.sleep(offset)
        start_time = time.time()

        while time.time() - start_time < 600:
            try:
                session.headers.update({"User-Agent": get_random_user_agent()})
                alert_details = await fetch_alert_details(session)
                if alert_details is None:
                    log_message("Current alert not interesting to us...", "INFO")
                    await asyncio.sleep(0.7)
                    continue

                if alert_details["title"] != last_alert_details.get("title"):
                    message = f"Title: {alert_details['title']}\nPrice: {alert_details['price']}\nCreated At: {alert_details['created_at']}\nCurrent Time: {alert_details['current_time']}"
                    await send_telegram_message(
                        message,
                        HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN,
                        HEDGEYE_SCRAPER_TELEGRAM_GRP,
                    )

                    signal_type = (
                        "Buy"
                        if "buy" in alert_details["title"].lower()
                        else (
                            "Sell"
                            if "sell" in alert_details["title"].lower()
                            else "None"
                        )
                    )
                    ticker_match = re.search(
                        r"\b([A-Z]{1,5})\b(?=\s*\$)", alert_details["title"]
                    )
                    ticker = ticker_match.group(0) if ticker_match else "-"

                    await send_ws_message(
                        {
                            "name": "Hedgeye",
                            "type": signal_type,
                            "ticker": ticker,
                            "sender": "hedgeye",
                        },
                        WS_SERVER_URL,
                    )

                    message += f"\nTicker: {ticker}"
                    log_message(f"New alert sent: {message}", "INFO")
                    last_alert_details = {
                        "title": alert_details["title"],
                        "created_at": alert_details["created_at"],
                    }
                await asyncio.sleep(0.6)
            except Exception as e:
                log_message(f"Error during monitoring: {str(e)}", "ERROR")
                await asyncio.sleep(0.7)

    while True:
        pre_market_login_time, market_open_time, market_close_time = (
            get_next_market_times()
        )
        current_time_edt = datetime.now(pytz.timezone("America/New_York"))

        if (
            pre_market_login_time <= current_time_edt < market_open_time
            or first_time_ever
        ):
            first_time_ever = False
            if not logged_in:
                log_message("Logging in or loading sessions...", "INFO")

                for i, (email, password) in enumerate(accounts):
                    session_filename = f"data/hedgeye_session_{i}.pkl"

                    if os.path.exists(session_filename):
                        try:
                            cookies = load_session(session_filename)
                            driver = Chrome(options=options)
                            driver.set_page_load_timeout(1200)
                            session = Session(driver=driver)
                            session.cookies.update(cookies)
                            sessions.append(session)
                            log_message(
                                f"Loaded session for account {i}: {email}", "INFO"
                            )
                        except Exception as e:
                            log_message(
                                f"Failed to load session for {email}: {str(e)}", "ERROR"
                            )
                            driver = Chrome(options=options)
                            driver.set_page_load_timeout(1200)
                            if login(driver, email, password):
                                session = Session(driver=driver)
                                session.transfer_driver_cookies_to_session()
                                sessions.append(session)
                                save_session(session, session_filename)
                                log_message(
                                    f"Logged in and saved session for account {i}: {email}",
                                    "INFO",
                                )
                            else:
                                log_message(
                                    f"Failed to login for account {i}: {email}",
                                    "ERROR",
                                )
                    else:
                        driver = Chrome(options=options)
                        driver.set_page_load_timeout(1200)
                        if login(driver, email, password):
                            session = Session(driver=driver)
                            session.transfer_driver_cookies_to_session()
                            sessions.append(session)
                            save_session(session, session_filename)
                            log_message(
                                f"Logged in and saved session for account {i}: {email}",
                                "INFO",
                            )
                        else:
                            log_message(
                                f"Failed to login for account {i}: {email}", "ERROR"
                            )

                    driver.quit()

                log_message("All accounts processed. Starting monitoring...", "INFO")
                logged_in = True

        elif market_open_time <= current_time_edt <= market_close_time:
            if not market_is_open:
                log_message("Market is open, starting monitoring...", "INFO")
                market_is_open = True

            tasks = []
            selected_sessions = random.sample(sessions, min(3, len(sessions)))

            for i, session in enumerate(selected_sessions):
                tasks.append(check_session(session, i * 0.2))
            await asyncio.gather(*tasks)

            selected_indices = [
                sessions.index(session) for session in selected_sessions
            ]

            log_message(f"Checked sessions with indices: {selected_indices}\n", "INFO")
            await asyncio.sleep(0.6)

        else:
            logged_in = False
            market_is_open = False
            await sleep_until_market_open()


if __name__ == "__main__":
    asyncio.run(monitor_feeds_async())
