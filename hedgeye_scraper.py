import asyncio
import json
import os
import pickle
import random
import re
import sys
import time
from asyncio import Queue as AsyncQueue
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from uuid import uuid4

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
RATE_LIMIT_PROXY_FILE = os.path.join(DATA_DIR, "hedgeye_rate_limited_proxy.json")
RATE_LIMIT_ACCOUNTS_FILE = os.path.join(DATA_DIR, "hedgeye_rate_limited_accounts.json")
# LAST_ALERT_FILE = os.path.join(DATA_DIR, "hedgeye_last_alert.json")
LAST_ALERT_FILE = os.path.join(DATA_DIR, "hedgeye_old_alert.json")

# Ensure data, cred directory exists
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs("cred", exist_ok=True)

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


@dataclass
class Task:
    session: Session
    email: str
    proxy: str
    start_time: float = 0.0

    def __hash__(self):
        return hash((self.email, self.proxy))

    def __eq__(self, other):
        if not isinstance(other, Task):
            return NotImplemented
        return self.email == other.email and self.proxy == other.proxy


class TaskQueue:
    def __init__(self, max_concurrent: int = 3):
        self.queue = asyncio.Queue()
        self.running_tasks: Set[Task] = set()
        self.max_concurrent = max_concurrent
        self.lock = asyncio.Lock()

    async def add_task(self, task: Task) -> bool:
        # Only add task if we're under the combined limit
        if self.queue.qsize() + len(self.running_tasks) >= self.max_concurrent:
            return False

        await self.queue.put(task)
        return True

    async def get_next_task(self) -> Optional[Task]:
        try:
            task = await self.queue.get()
            async with self.lock:
                task.start_time = time.time()
                self.running_tasks.add(task)
            return task
        except asyncio.QueueEmpty:
            return None

    async def complete_task(self, task: Task):
        async with self.lock:
            if task in self.running_tasks:
                self.running_tasks.remove(task)
                self.queue.task_done()

    def get_running_count(self) -> int:
        return len(self.running_tasks)

    def get_queue_size(self) -> int:
        return self.queue.qsize()


class TelegramQueue:
    def __init__(self):
        self.queue = AsyncQueue()
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._process_queue())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _process_queue(self):
        while True:
            message = await self.queue.get()
            try:
                await send_telegram_message(
                    message["text"],
                    HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN,
                    HEDGEYE_SCRAPER_TELEGRAM_GRP,
                )
            except Exception as e:
                log_message(f"Error sending Telegram message: {str(e)}", "ERROR")
            finally:
                self.queue.task_done()

    async def send_message(self, message):
        await self.queue.put(message)


class ProxyManager:
    def __init__(self, proxies: List[str]):
        self.proxies = proxies
        self.current_index = 0
        self.rate_limited: Dict[str, datetime] = self._load_rate_limited()

    def _load_rate_limited(self) -> Dict[str, datetime]:
        if os.path.exists(RATE_LIMIT_PROXY_FILE):
            with open(RATE_LIMIT_PROXY_FILE, "r") as f:
                rate_limited = json.load(f)
                return {k: datetime.fromisoformat(v) for k, v in rate_limited.items()}
        return {}

    def _save_rate_limited(self):
        with open(RATE_LIMIT_PROXY_FILE, "w") as f:
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
        self.rate_limited[proxy] = datetime.now()
        self._save_rate_limited()

    def clear_rate_limits(self):
        self.rate_limited.clear()
        if os.path.exists(RATE_LIMIT_PROXY_FILE):
            os.remove(RATE_LIMIT_PROXY_FILE)
        log_message("All proxy rate limits cleared", "INFO")


class AccountManager:
    def __init__(self, accounts: List[Tuple[str, str]]):
        self.accounts = accounts
        self.rate_limited: Set[str] = self._load_rate_limited()
        self.currently_running: Set[str] = set()
        self.lock = asyncio.Lock()

    def _load_rate_limited(self) -> Set[str]:
        if os.path.exists(RATE_LIMIT_ACCOUNTS_FILE):
            with open(RATE_LIMIT_ACCOUNTS_FILE, "r") as f:
                return set(json.load(f))
        return set()

    def _save_rate_limited(self):
        with open(RATE_LIMIT_ACCOUNTS_FILE, "w") as f:
            json.dump(list(self.rate_limited), f)

    async def get_available_accounts(self, count: int) -> List[Tuple[str, str]]:
        async with self.lock:
            available = [
                acc
                for acc in self.accounts
                if acc[0] not in self.rate_limited
                and acc[0] not in self.currently_running
            ]
            selected = random.sample(available, min(count, len(available)))
            self.currently_running.update(email for email, _ in selected)
            return selected

    async def release_account(self, email: str):
        async with self.lock:
            if email in self.currently_running:
                self.currently_running.remove(email)

    def mark_rate_limited(self, email: str):
        self.rate_limited.add(email)
        self._save_rate_limited()

    def clear_rate_limits(self):
        self.rate_limited.clear()
        self.currently_running.clear()
        if os.path.exists(RATE_LIMIT_ACCOUNTS_FILE):
            os.remove(RATE_LIMIT_ACCOUNTS_FILE)
        log_message("All account rate limits cleared", "INFO")


class SessionInfo:
    def __init__(self, session, email):
        self.session = session
        self.email = email


def get_random_user_agent():
    return random.choice(user_agents)


def random_scroll(driver):
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


def archive_alert_parser(articles, fetch_time, current_time):
    result = []

    for article in articles:
        try:
            title = article.find(
                "h2", class_="thumbnail-article-quarter__title"
            ).get_text(strip=True)

            date_text = article.find(
                "div", class_="thumbnail-article-quarter__date"
            ).get_text(strip=True)

            date_format = "%m/%d/%y %I:%M %p EST"
            created_at = datetime.strptime(date_text, date_format)
            created_at_utc = created_at.astimezone(pytz.utc)

            created_at_edt = created_at_utc.astimezone(
                pytz.timezone("America/New_York")
            )

            result.append(
                {
                    "title": title,
                    "created_at": created_at_edt,
                    "current_time": current_time,
                    "fetch_time": fetch_time,
                }
            )
        except:
            pass

    return result


async def fetch_alert_details(session, proxy_raw):
    try:
        ip, port = proxy_raw.split(":")
        proxy = f"http://{ip}:{port}"
        timestamp = int(time.time() * 10000)
        cache_uuid = uuid4()

        # Create temp header to use so that we don't modify the actual one
        temp_headers = session.headers
        temp_headers["Cache-Control"] = (
            "no-cache, no-store, max-age=0, must-revalidate, private"
        )
        temp_headers["Connection"] = "keep-alive"
        temp_headers["cache-timestamp"] = str(timestamp)
        temp_headers["cache-uuid"] = str(cache_uuid)

        start_time = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        async with aiohttp.ClientSession() as aio_session:
            async with aio_session.get(
                # f"https://app.hedgeye.com/feed_items/all?with_category=22-real-time-alerts&timstamp={str(timestamp + 10)}",
                f"https://app.hedgeye.com/research_archives?with_category=22-real-time-alerts&month={today}&timestamp={str(timestamp + 10)}",
                headers=temp_headers,
                cookies=session.cookies,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=2),
            ) as response:
                if response.status == 429:
                    raise Exception("Rate limited")
                html = await response.text()

        soup = BeautifulSoup(html, "html.parser")

        # Method 1: https://app.hedgeye.com/feed_items/all
        # alert_title = soup.select_one(".article__header")
        # if not alert_title:
        #     return None
        # alert_title = alert_title.get_text(strip=True)
        #
        # alert_price = soup.select_one(".currency.se-live-or-close-price")
        # if not alert_price:
        #     return None
        # alert_price = alert_price.get_text(strip=True)
        #
        #
        # created_at_utc = soup.select_one("time[datetime]")["datetime"]
        # created_at = datetime.fromisoformat(created_at_utc.replace("Z", "+00:00"))
        # created_at_edt = created_at.astimezone(pytz.timezone("America/New_York"))

        current_time_edt = datetime.now(pytz.utc).astimezone(
            pytz.timezone("America/New_York")
        )
        fetch_time = time.time() - start_time

        # return {
        #     "title": alert_title,
        #     "price": alert_price,
        #     "created_at": created_at_edt,
        #     "current_time": current_time_edt,
        #     "fetch_time": fetch_time,
        # }

        # Method 2: https://app.hedgeye.com/research_archives
        articles = soup.find_all("div", class_="thumbnail-article__details")
        results = archive_alert_parser(articles, fetch_time, current_time_edt)

        return results

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


def load_old_alert():
    if os.path.exists(LAST_ALERT_FILE):
        with open(LAST_ALERT_FILE, "r") as f:
            return json.load(f)
    return []


async def get_public_ip(proxy):
    ip_check_url = "https://api.ipify.org?format=text"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ip_check_url, proxy=proxy) as response:
                if response.status == 200:
                    ip = await response.text()
                    return ip.strip()
                return f"Code: {response.status}"
    except Exception as e:
        return f"Error: {e}"


def save_session(session, filename):
    with open(filename, "wb") as f:
        pickle.dump(session.cookies, f)


def load_session(filename):
    with open(filename, "rb") as f:
        return pickle.load(f)


async def process_task(
    task: Task,
    task_queue: TaskQueue,
    account_manager: AccountManager,
    proxy_manager: ProxyManager,
    telegram_queue: TelegramQueue,
    last_alert_lock: asyncio.Lock,
):
    try:
        start_time = time.time()
        # alert_details = await fetch_alert_details(task.session, task.proxy)
        results = await fetch_alert_details(task.session, task.proxy)

        if results is None:
            log_message("fetch_alert_details returns none", "WARNING")
            return

        for result in results:
            log_message(
                f"fetch_result took {result['fetch_time']:.2f} seconds. for {task.email}, {task.proxy}",
                "INFO",
            )

            # Use lock for thread-safe comparison
            async with last_alert_lock:
                old_alerts = load_old_alert()
                is_new_alert = not old_alerts or result["title"] in old_alerts
                if is_new_alert:
                    signal_type = (
                        "Buy"
                        if "buy" in result["title"].lower()
                        else "Sell" if "sell" in result["title"].lower() else "None"
                    )
                    ticker_match = re.search(
                        r"\b([A-Z]{1,5})\b(?=\s*\$)", result["title"]
                    )
                    ticker = ticker_match.group(0) if ticker_match else "-"

                    log_message(
                        f"Trying to send new alert, Title - {result['title']}, Proxy - {task.proxy}",
                        "INFO",
                    )
                    # Send WebSocket message immediately
                    await send_ws_message(
                        {
                            "name": "Hedgeye",
                            "type": signal_type,
                            "ticker": ticker,
                            "sender": "hedgeye",
                        },
                        WS_SERVER_URL,
                    )

                    # Queue Telegram message separately
                    message = (
                        f"Title: {result['title']}\n"
                        f"Created At: {result['created_at'].strftime('%Y-%m-%d %H:%M:%S %Z%z')}\n"
                        f"Current Time: {result['current_time'].strftime('%Y-%m-%d %H:%M:%S %Z%z')}\n"
                        f"Fetch Time: {result['fetch_time']:.2f}s"
                    )
                    await telegram_queue.send_message({"text": message})

                    old_alerts.append(result["title"])
                    with open(LAST_ALERT_FILE, "w") as f:
                        json.dump(
                            old_alerts,
                            f,
                        )

                    total_time = time.time() - start_time
                    log_message(
                        f"New alert processed in {total_time:.2f}s - {result['title']}",
                        "INFO",
                    )

                if result["fetch_time"] > 1.5:
                    log_message(
                        f"Slow fetch detected Publid IP: {result['fetch_time']} seconds",
                        "WARNING",
                    )

        #
        # log_message(
        #     f"fetch_alert_details took {alert_details['fetch_time']:.2f} seconds. for {task.email}, {task.proxy}",
        #     "INFO",
        # )
        #
        # # Use lock for thread-safe comparison
        # async with last_alert_lock:
        #     last_alert = load_last_alert()
        #     is_new_alert = not last_alert or alert_details["title"] != last_alert.get(
        #         "title"
        #     )
        #
        #     if is_new_alert:
        #         signal_type = (
        #             "Buy"
        #             if "buy" in alert_details["title"].lower()
        #             else "Sell" if "sell" in alert_details["title"].lower() else "None"
        #         )
        #         ticker_match = re.search(
        #             r"\b([A-Z]{1,5})\b(?=\s*\$)", alert_details["title"]
        #         )
        #         ticker = ticker_match.group(0) if ticker_match else "-"
        #
        #         log_message(
        #             f"Trying to send new alert, Title - {alert_details['title']}, Proxy - {task.proxy}",
        #             "INFO",
        #         )
        #         # Send WebSocket message immediately
        #         await send_ws_message(
        #             {
        #                 "name": "Hedgeye",
        #                 "type": signal_type,
        #                 "ticker": ticker,
        #                 "sender": "hedgeye",
        #             },
        #             WS_SERVER_URL,
        #         )
        #
        #         # Queue Telegram message separately
        #         message = (
        #             f"Title: {alert_details['title']}\n"
        #             f"Price: {alert_details['price']}\n"
        #             f"Created At: {alert_details['created_at'].strftime('%Y-%m-%d %H:%M:%S %Z%z')}\n"
        #             f"Current Time: {alert_details['current_time'].strftime('%Y-%m-%d %H:%M:%S %Z%z')}\n"
        #             f"Fetch Time: {alert_details['fetch_time']:.2f}s"
        #         )
        #         await telegram_queue.send_message({"text": message})
        #
        #         with open(LAST_ALERT_FILE, "w") as f:
        #             json.dump(
        #                 {
        #                     "title": alert_details["title"],
        #                     "price": alert_details["price"],
        #                     "created_at": alert_details["created_at"].isoformat(),
        #                 },
        #                 f,
        #             )
        #
        #         total_time = time.time() - start_time
        #         log_message(
        #             f"New alert processed in {total_time:.2f}s - {alert_details['title']}",
        #             "INFO",
        #         )
        #
        #     if alert_details["fetch_time"] > 1.5:
        #         public_ip = await get_public_ip(f"http://{task.proxy}")
        #         log_message(
        #             f"Slow fetch detected Publid IP: {public_ip} seconds", "WARNING"
        #         )

    except Exception as e:
        if "Rate limited" in str(e):
            proxy_manager.mark_rate_limited(task.proxy)
            log_message(f"Rate limited: Proxy {task.proxy}", "WARNING")
        else:
            log_message(f"Error during monitoring: {str(e)}", "ERROR")
    finally:
        await account_manager.release_account(task.email)
        await task_queue.complete_task(task)


async def task_scheduler(
    task_queue: TaskQueue,
    account_manager: AccountManager,
    proxy_manager: ProxyManager,
    telegram_queue: TelegramQueue,
    last_alert_lock: asyncio.Lock,
):
    while True:
        try:
            if task_queue.get_running_count() < task_queue.max_concurrent:
                task = await task_queue.get_next_task()
                if task:
                    asyncio.create_task(
                        process_task(
                            task,
                            task_queue,
                            account_manager,
                            proxy_manager,
                            telegram_queue,
                            last_alert_lock,
                        )
                    )
            await asyncio.sleep(0.2)
        except Exception as e:
            log_message(f"Error in task scheduler: {str(e)}", "ERROR")
            await asyncio.sleep(1)


async def monitor_feeds_async():
    accounts, proxies = load_credentials()
    account_manager = AccountManager(accounts)
    proxy_manager = ProxyManager(proxies)
    task_queue = TaskQueue(max_concurrent=3)
    telegram_queue = TelegramQueue()
    last_alert_lock = asyncio.Lock()
    market_is_open = False
    logged_in = False
    first_time_ever = True
    sessions = []

    # Start the Telegram queue processor and the task scheduler
    await telegram_queue.start()
    scheduler_task = asyncio.create_task(
        task_scheduler(
            task_queue, account_manager, proxy_manager, telegram_queue, last_alert_lock
        )
    )

    try:
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

                    async def process_account(email, password, index):
                        session_filename = f"data/hedgeye_session_{index}.pkl"

                        try:
                            if os.path.exists(session_filename):
                                try:
                                    cookies = load_session(session_filename)
                                    driver = Chrome(options=options)
                                    driver.set_page_load_timeout(1200)
                                    session = Session(driver=driver)
                                    session.cookies.update(cookies)
                                    sessions.append(SessionInfo(session, email))
                                    log_message(
                                        f"Loaded session for account {index}: {email}",
                                        "INFO",
                                    )
                                    return True
                                except Exception as e:
                                    log_message(
                                        f"Failed to load session for {email}: {str(e)}",
                                        "ERROR",
                                    )

                            # If loading failed or file doesn't exist, try fresh login
                            driver = Chrome(options=options)
                            driver.set_page_load_timeout(1200)
                            if login(driver, email, password):
                                session = Session(driver=driver)
                                session.transfer_driver_cookies_to_session()
                                sessions.append(SessionInfo(session, email))
                                save_session(session, session_filename)
                                log_message(
                                    f"Logged in and saved session for account {index}: {email}",
                                    "INFO",
                                )
                                return True
                            else:
                                log_message(
                                    f"Failed to login for account {index}: {email}",
                                    "ERROR",
                                )
                                return False
                        finally:
                            if "driver" in locals():
                                driver.quit()

                    # Process accounts concurrently with rate limiting
                    tasks = []
                    for i, (email, password) in enumerate(accounts):
                        if i > 0:
                            await asyncio.sleep(1)
                        task = asyncio.create_task(process_account(email, password, i))
                        tasks.append(task)

                    await asyncio.gather(*tasks)
                    log_message(
                        "All accounts processed. Starting monitoring...", "INFO"
                    )
                    logged_in = True

            elif market_open_time <= current_time_edt <= market_close_time:
                if not market_is_open:
                    proxy_manager.clear_rate_limits()
                    account_manager.clear_rate_limits()
                    log_message("Market is open, starting monitoring...", "INFO")
                    market_is_open = True

                try:
                    selected_accounts = await account_manager.get_available_accounts(1)

                    if selected_accounts:
                        email, _ = selected_accounts[0]
                        session_info = next(
                            (s for s in sessions if s.email == email), None
                        )

                        if session_info:
                            proxy = proxy_manager.get_next_proxy()
                            task = Task(session_info.session, email, proxy)

                            if not await task_queue.add_task(task):
                                await account_manager.release_account(email)
                except Exception as e:
                    log_message(f"Error during monitoring cycle: {str(e)}", "ERROR")
                finally:
                    await asyncio.sleep(1.1)

            else:
                logged_in = False
                market_is_open = False
                await sleep_until_market_open()

    except Exception as e:
        log_message(f"Critical error in monitor_feeds_async: {e}", "CRITICAL")
    finally:
        # Cleanup
        scheduler_task.cancel()
        await telegram_queue.stop()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


def main():
    if not all(
        [
            HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN,
            HEDGEYE_SCRAPER_TELEGRAM_GRP,
            WS_SERVER_URL,
        ]
    ):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(monitor_feeds_async())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
