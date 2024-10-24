import asyncio
import json
import os
import pickle
import random
import re
import time
from datetime import datetime
from typing import Dict, List, Set, Tuple

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requestium import Session
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
DATA_DIR = "data"
RATE_LIMIT_FILE = os.path.join(DATA_DIR, "rate_limited.json")
LAST_ALERT_FILE = os.path.join(DATA_DIR, "last_alert.json")

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

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


class ProxyManager:
    def __init__(self, proxies: List[str]):
        self.proxies = proxies
        self.current_index = 0
        self.rate_limited: Dict[str, datetime] = self._load_rate_limited()

    def _load_rate_limited(self) -> Dict[str, datetime]:
        if os.path.exists(RATE_LIMIT_FILE):
            with open(RATE_LIMIT_FILE, "r") as f:
                rate_limited = json.load(f)
                return {k: datetime.fromisoformat(v) for k, v in rate_limited.items()}
        return {}

    def _save_rate_limited(self):
        with open(RATE_LIMIT_FILE, "w") as f:
            rate_limited = {k: v.isoformat() for k, v in self.rate_limited.items()}
            json.dump(rate_limited, f)

    def get_next_proxy(self) -> str:
        current_time = datetime.now()

        expired_proxies = [
            proxy
            for proxy, limit_time in self.rate_limited.items()
            if (current_time - limit_time).total_seconds() >= 900  # 15 minutes
        ]

        for proxy in expired_proxies:
            del self.rate_limited[proxy]
            log_message(
                f"Proxy {proxy} removed from rate limits (15-minute expired)", "INFO"
            )

        if expired_proxies:
            self._save_rate_limited()

        available_proxies = [
            proxy for proxy in self.proxies if proxy not in self.rate_limited
        ]

        if not available_proxies:
            raise Exception("No available proxies")

        proxy = random.choice(available_proxies)
        return proxy

    def mark_rate_limited(self, proxy: str):
        # Store the current time when the proxy is rate limited
        self.rate_limited[proxy] = datetime.now()
        self._save_rate_limited()

    def clear_rate_limits(self):
        self.rate_limited.clear()
        if os.path.exists(RATE_LIMIT_FILE):
            os.remove(RATE_LIMIT_FILE)
        log_message("All proxy rate limits cleared", "INFO")

    def get_rate_limited_count(self) -> int:
        """Return the number of currently rate-limited proxies"""
        return len(self.rate_limited)

    def get_available_count(self) -> int:
        """Return the number of currently available proxies"""
        return len(self.proxies) - len(self.rate_limited)


class AccountManager:
    def __init__(self, accounts: List[Tuple[str, str]]):
        self.accounts = accounts
        self.rate_limited: Set[str] = self._load_rate_limited()

    def _load_rate_limited(self) -> Set[str]:
        rate_limit_file = os.path.join(DATA_DIR, "rate_limited_accounts.json")
        if os.path.exists(rate_limit_file):
            with open(rate_limit_file, "r") as f:
                return set(json.load(f))
        return set()

    def _save_rate_limited(self):
        rate_limit_file = os.path.join(DATA_DIR, "rate_limited_accounts.json")
        with open(rate_limit_file, "w") as f:
            json.dump(list(self.rate_limited), f)

    def get_available_accounts(self, count: int) -> List[Tuple[str, str]]:
        available = [acc for acc in self.accounts if acc[0] not in self.rate_limited]
        return random.sample(available, min(count, len(available)))

    def mark_rate_limited(self, email: str):
        self.rate_limited.add(email)
        self._save_rate_limited()

    def clear_rate_limits(self):
        self.rate_limited.clear()
        rate_limit_file = os.path.join(DATA_DIR, "rate_limited_accounts.json")
        if os.path.exists(rate_limit_file):
            os.remove(rate_limit_file)
        log_message("All account rate limits cleared", "INFO")


class SessionInfo:
    def __init__(self, session, email):
        self.session = session
        self.email = email


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


def load_credentials() -> Tuple[List[Tuple[str, str]], List[str]]:
    with open("cred/hedgeye_credentials.json", "r") as f:
        data = json.load(f)
        accounts = [(acc["email"], acc["password"]) for acc in data["accounts"]]
        proxies = data["proxies"]
        return accounts, proxies


async def fetch_alert_details(session, proxy_raw):
    try:
        ip, port = proxy_raw.split(":")
        proxy = f"http://{ip}:{port}"

        async with aiohttp.ClientSession() as aio_session:
            async with aio_session.get(
                "https://app.hedgeye.com/feed_items/all",
                headers=session.headers,
                cookies=session.cookies,
                proxy=proxy,
            ) as response:
                if response.status == 429:
                    raise Exception("Rate limited")
                html = await response.text()

        soup = BeautifulSoup(html, "html.parser")
        alert_title = soup.select_one(".article__header")
        if not alert_title:
            return None
        alert_title = alert_title.get_text(strip=True)

        alert_price = soup.select_one(".currency.se-live-or-close-price")
        if not alert_price:
            return None
        alert_price = alert_price.get_text(strip=True)

        created_at_utc = soup.select_one("time[datetime]")["datetime"]
        created_at = datetime.fromisoformat(created_at_utc.replace("Z", "+00:00"))
        edt = pytz.timezone("America/New_York")
        created_at_edt = created_at.astimezone(edt)
        current_time_edt = datetime.now(pytz.utc).astimezone(edt)

        alert_details = {
            "title": alert_title,
            "price": alert_price,
            "created_at": created_at_edt.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
            "current_time": current_time_edt.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
        }

        # Save last alert details
        with open(LAST_ALERT_FILE, "w") as f:
            json.dump(alert_details, f)

        return alert_details

    except Exception as e:
        if "Rate limited" in str(e):
            raise
        log_message(f"Error fetching alert details: {str(e)}", "ERROR")
        return None


def load_last_alert():
    if os.path.exists(LAST_ALERT_FILE):
        with open(LAST_ALERT_FILE, "r") as f:
            return json.load(f)
    return {}


def save_session(session, filename):
    with open(filename, "wb") as f:
        pickle.dump(session.cookies, f)


def load_session(filename):
    with open(filename, "rb") as f:
        return pickle.load(f)


async def monitor_feeds_async():
    accounts, proxies = load_credentials()
    account_manager = AccountManager(accounts)
    proxy_manager = ProxyManager(proxies)
    last_alert_details = load_last_alert()
    market_is_open = False
    logged_in = False
    first_time_ever = True
    sessions = []

    async def check_session(session, email, proxy):
        nonlocal last_alert_details
        try:
            alert_details = await fetch_alert_details(session, proxy)

            if alert_details is None:
                return

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
                    else "Sell" if "sell" in alert_details["title"].lower() else "None"
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
                last_alert_details = alert_details

        except Exception as e:
            if "Rate limited" in str(e):
                proxy_manager.mark_rate_limited(proxy)
                log_message(f"Rate limited: Proxy {proxy}", "WARNING")
            else:
                log_message(f"Error during monitoring: {str(e)}", "ERROR")
        finally:
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
                            sessions.append(SessionInfo(session, email))
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
                                sessions.append(SessionInfo(session, email))
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
                            sessions.append(SessionInfo(session, email))
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
                proxy_manager.clear_rate_limits()
                account_manager.clear_rate_limits()
                log_message("Market is open, starting monitoring...", "INFO")
                market_is_open = True

            try:
                selected_accounts = account_manager.get_available_accounts(3)

                if not selected_accounts or len(selected_accounts) == 0:
                    raise Exception("No available Accounts")

                tasks = []

                for email, _ in selected_accounts:
                    session_info = next((s for s in sessions if s.email == email), None)
                    if session_info:
                        proxy = proxy_manager.get_next_proxy()
                        tasks.append(check_session(session_info.session, email, proxy))
                        await asyncio.sleep(0.2)

                if tasks:
                    await asyncio.gather(*tasks)
                await asyncio.sleep(0.7)

            except Exception as e:
                log_message(f"Error during monitoring cycle: {str(e)}", "ERROR")
                await asyncio.sleep(1)

        else:
            logged_in = False
            market_is_open = False
            await sleep_until_market_open()


if __name__ == "__main__":
    asyncio.run(monitor_feeds_async())
