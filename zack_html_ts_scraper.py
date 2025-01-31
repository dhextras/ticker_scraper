import asyncio
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from time import time
from typing import Dict, NamedTuple, Set

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()


class ZacksService(NamedTuple):
    name: str
    ts_id: str
    newsletter_id: str


# Constants
TELEGRAM_BOT_TOKEN = os.getenv("ZACKS_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("ZACKS_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
ZACKS_USERNAME = os.getenv("ZACKS_USERNAME")
ZACKS_PASSWORD = os.getenv("ZACKS_PASSWORD")
CHECK_INTERVAL = 0.2  # seconds

DATA_DIR = Path("data")
CRED_DIR = Path("cred")
PROXY_FILE = CRED_DIR / "proxies.json"

# Services configuration
ZACKS_SERVICES = [
    ZacksService("Home Run Investor", "6", "243"),
    ZacksService("Stocks Under 10", "13", "260"),
    ZacksService("Counterstrike Trader", "18", "268"),
    ZacksService("Insider Trader", "9", "244"),
    ZacksService("Surprise Trader", "14", "202"),
    ZacksService("TAZR", "10", "255"),
    ZacksService("Blockchain Innovators", "21", "293"),
    ZacksService("Healthcare Innovators", "19", "274"),
    ZacksService("Marijuana Innovators", "25", "294"),
    ZacksService("Technology Innovators", "20", "275"),
]


# Global variables
alert_locks: Dict[str, asyncio.Lock] = {
    service.name: asyncio.Lock() for service in ZACKS_SERVICES
}
proxy_lock = asyncio.Lock()
session_lock = asyncio.Lock()
active_proxies: Set[str] = set()
previous_alerts: Dict[str, list] = {}
proxy_sessions: Dict[str, aiohttp.ClientSession] = {}


def get_alerts_file(service_name):
    """Get the alerts file path for a specific service"""
    return (
        DATA_DIR / f"zacks_ts_portfolio/{service_name.lower().replace(' ', '_')}.json"
    )


def load_proxies():
    """Load proxies from json file"""
    try:
        with open(PROXY_FILE, "r") as f:
            data = json.load(f)
            return data["zacks_ts"]
    except Exception as e:
        log_message(f"Error loading proxies: {e}", "ERROR")
        return []


async def get_available_proxy(proxies):
    """Get a random available proxy that isn't currently in use"""
    async with proxy_lock:
        available_proxies = set(proxies) - active_proxies
        if not available_proxies:
            await asyncio.sleep(0.5)
            log_message(
                "No available proxy to choose from, be carefull it might go to inifinite loop",
                "ERROR",
            )
            return await get_available_proxy(proxies)

        proxy = random.choice(list(available_proxies))
        active_proxies.add(proxy)
        return proxy


async def release_proxy(proxy):
    """Release a proxy back to the available pool"""
    proxy = proxy[7:]  # Remove the http:// in the proxy
    async with proxy_lock:
        active_proxies.discard(proxy)


def load_saved_alerts(service_name):
    """Load previously saved alerts for a specific service"""
    alerts_file = get_alerts_file(service_name)
    try:
        DATA_DIR.mkdir(exist_ok=True)
        if alerts_file.exists():
            with open(alerts_file, "r") as f:
                return json.load(f)
        return []
    except Exception as e:
        log_message(f"Error loading saved alerts for {service_name}: {e}", "ERROR")
        return []


async def save_alerts(service_name, data):
    """Save alerts for a specific service with proper locking"""
    async with alert_locks[service_name]:
        try:
            alerts_file = get_alerts_file(service_name)
            DATA_DIR.mkdir(exist_ok=True)
            with open(alerts_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log_message(f"Error saving alerts for {service_name}: {e}", "ERROR")


def process_raw_data(html, service):
    """
    Process HTML and extract portfolio data from main, addition, and deletion tables.
    Returns consolidated portfolio data after applying additions and removals.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Helper function to extract data from a table
        def extract_table_data(table_id):
            table = soup.find("table", id=table_id)
            if not table:
                return []

            tbody = table.find("tbody")
            if not tbody:
                return []

            rows = tbody.find_all("tr")
            extracted_data = []

            for row in rows:
                try:
                    symbol_elem = row.find("td", class_="symbol")
                    if not symbol_elem:
                        continue

                    symbol_container = symbol_elem.find(
                        "a", class_="hoverquote-container-od"
                    )
                    if not symbol_container or "rel" not in symbol_container.attrs:
                        continue

                    symbol_list = symbol_container["rel"]
                    if len(symbol_list) < 1:
                        continue

                    data = {
                        "company": row.find("th", class_="company")["title"],
                        "symbol": symbol_list[0],
                        "date_added": row.find("td", class_="date-add").text.strip(),
                        "price_added": row.find("td", class_="price-add").text.strip(),
                    }
                    extracted_data.append(data)
                except Exception as e:
                    log_message(
                        f"Error processing row for {service.name}: {e}", "ERROR"
                    )
                    continue

            return extracted_data

        # Extract data from all three tables
        main_data = extract_table_data("port_sort")
        additions = extract_table_data("add_sort")
        deletions = extract_table_data("del_sort")

        main_symbols = {item["symbol"] for item in main_data}

        # Process additions - add only if not already in main portfolio
        for addition in additions:
            if addition["symbol"] not in main_symbols:
                main_data.append(addition)
                main_symbols.add(addition["symbol"])

        # Process deletions - remove if present in main portfolio
        main_data = [
            item
            for item in main_data
            if item["symbol"] not in {deletion["symbol"] for deletion in deletions}
        ]

        return main_data

    except Exception as e:
        log_message(f"Failed to process raw html data for {service.name}: {e}", "ERROR")
        return []


async def create_session(proxy):
    """Create and return a new session with login using specific proxy"""
    try:
        session = aiohttp.ClientSession()

        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.zacks.com",
            "referer": "https://www.zacks.com/tradingservices/index.php?ts_id=18&newsletterid=268",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.37 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }

        params = {
            "ts_id": "18",
            "newsletterid": "268",
        }

        data = {
            "force_login": "true",
            "username": ZACKS_USERNAME,
            "password": ZACKS_PASSWORD,
            "remember_me": "on",
        }

        try:
            async with session.post(
                "https://www.zacks.com/tradingservices/index.php",
                headers=headers,
                params=params,
                data=data,
                proxy=proxy,
            ) as response:
                if response.status != 200:
                    await session.close()
                    log_message(
                        f"Login failed with status code: {response.status}", "ERROR"
                    )
                    return None

        except Exception as e:
            await session.close()
            log_message(f"Error during login with proxy {proxy}: {e}", "ERROR")
            return None

        return session
    except Exception as e:
        log_message(f"Error creating session with proxy {proxy}: {e}", "ERROR")
        return None


async def get_or_create_session(proxy):
    """Get existing session for proxy or create new one"""
    async with session_lock:
        if proxy in proxy_sessions and not proxy_sessions[proxy].closed:
            return proxy_sessions[proxy]

        session = await create_session(proxy)
        if session:
            proxy_sessions[proxy] = session
        return session


async def fetch_service_data(service, proxy):
    """Fetch data for a specific service using the provided proxy"""
    session = await get_or_create_session(proxy)
    if not session:
        return None

    url = "https://www.zacks.com/tradingservices/index.php"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.37 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    params = {
        "ts_id": service.ts_id,
        "newsletterid": service.newsletter_id,
    }

    try:
        async with session.get(
            url, headers=headers, params=params, proxy=proxy
        ) as response:
            await release_proxy(proxy)

            if response.status != 200:
                if response.status in [401, 403]:
                    # Session expired, remove it
                    async with session_lock:
                        if proxy in proxy_sessions:
                            await proxy_sessions[proxy].close()
                            del proxy_sessions[proxy]
                    return None
                log_message(
                    f"Failed to fetch {service.name}. Status: {response.status}",
                    "ERROR",
                )
                return None
            return await response.text()
    except Exception as e:
        log_message(f"Error fetching {service.name}: {e}", "ERROR")
        return None


def extract_changes(old_alerts, new_alerts):
    """Extract tickers that have been either added or removed"""
    try:
        old_symbols = [alert["symbol"] for alert in old_alerts]
        new_symbols = [alert["symbol"] for alert in new_alerts]

        changes = []
        changes.extend(
            [("Buy", symbol) for symbol in new_symbols if symbol not in old_symbols]
        )
        changes.extend(
            [("Sell", symbol) for symbol in old_symbols if symbol not in new_symbols]
        )

        return changes
    except Exception as e:
        log_message(f"Error extracting changes: {e}", "ERROR")
        return []


async def process_service(service, proxy):
    """Process a single service and handle its alerts"""
    try:
        proxy = f"http://{proxy}"
        raw_html = await fetch_service_data(service, proxy)
        if raw_html is None:
            return False

        portfolio_alerts = process_raw_data(raw_html, service)
        if not portfolio_alerts:
            return False

        changes = extract_changes(
            previous_alerts.get(service.name, []), portfolio_alerts
        )
        if changes:
            current_time = datetime.now(pytz.utc)

            # Send WebSocket messages concurrently
            # ws_tasks = [
            #     send_ws_message(
            #         {
            #             "name": f"Zacks TS - {service.name}",
            #             "type": action,
            #             "ticker": ticker,
            #             "sender": "zacks",
            #         },
            #         WS_SERVER_URL,
            #     )
            #     for action, ticker in changes
            # ]
            # await asyncio.gather(*ws_tasks)
            #
            # Prepare and send Telegram message
            changes_text = "\n".join(
                [f"- {action}: {ticker}" for action, ticker in changes]
            )
            message = (
                f"<b>New Zacks Trading Service Alert - {service.name}!</b>\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"<b>Changed Tickers:</b>\n{changes_text}"
            )

            previous_alerts[service.name] = portfolio_alerts
            await asyncio.gather(
                send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID),
                save_alerts(service.name, portfolio_alerts),
            )

            log_message(
                f"Processed changes for {service.name} - {len(changes)} changes"
            )

        return True

    except Exception as e:
        log_message(f"Error processing service {service.name}: {e}", "ERROR")
        return False


async def process_batch(proxies):
    """Process all services concurrently using available proxies"""
    tasks = []
    for service in ZACKS_SERVICES:
        proxy = await get_available_proxy(proxies)
        tasks.append(process_service(service, proxy))

    results = await asyncio.gather(*tasks)
    return all(results)


async def run_scraper():
    """Main scraper loop that respects market hours"""
    global previous_alerts

    for service in ZACKS_SERVICES:
        previous_alerts[service.name] = load_saved_alerts(service.name)

    proxies = load_proxies()
    if not proxies:
        log_message("No proxies available", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting service monitoring...")

        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))
            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...")
                # Close all sessions
                async with session_lock:
                    for session in proxy_sessions.values():
                        if not session.closed:
                            await session.close()
                    proxy_sessions.clear()
                break

            start_time = time()
            log_message("Starting new scan cycle...")

            try:
                success = await process_batch(proxies)
                if not success:
                    log_message("Some services failed to process", "INFO")

                log_message(
                    f"Scan cycle completed in {time() - start_time:.2f} seconds"
                )
                await asyncio.sleep(CHECK_INTERVAL)

            except Exception as e:
                log_message(f"Error in scraper loop: {e}", "ERROR")
                # Close all sessions on error
                async with session_lock:
                    for session in proxy_sessions.values():
                        if not session.closed:
                            await session.close()
                    proxy_sessions.clear()
                await asyncio.sleep(1)


def main():
    if not all(
        [
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            WS_SERVER_URL,
            ZACKS_USERNAME,
            ZACKS_PASSWORD,
        ]
    ):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        for service in ZACKS_SERVICES:
            if service.name in previous_alerts:
                asyncio.run(save_alerts(service.name, previous_alerts[service.name]))
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
