import asyncio
import json
import os
import random
import re
import sys
import warnings
from time import time
from typing import Dict, List, NamedTuple, Optional, Set

import requests
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from dotenv import load_dotenv

from utils.bypass_cloudflare import bypasser
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)


# Constants
CHECK_INTERVAL = 60  # seconds
DATA_DIR = "data/tradesmith"
CRED_DIR = "cred"
SESSION_FILE = "data/tradesmith_session.json"
PROCESSED_DATA_FILE = "data/tradesmith_processed.json"
BASE_URL = "https://publishers.tradesmith.com/Preview/Preview"
TELEGRAM_BOT_TOKEN = os.getenv("OXFORDCLUB_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("OXFORDCLUB_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
PROXY_FILE = CRED_DIR + "/proxies.json"

# Create necessary directories
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CRED_DIR, exist_ok=True)


class TradeSmithService(NamedTuple):
    name: str
    guid: str


# Services configuration
TRADESMITH_SERVICES = [
    TradeSmithService("Oxford X Portfolio", "1e5063c3-22c3-474e-be6f-1aa12ed135da"),
    TradeSmithService(
        "Oxford Centurion Portfolio", "fe9030df-c069-4cdd-9be0-0d2337a0527c"
    ),
    TradeSmithService(
        "The Oxford Trading Portfolio", "f4aa9836-a013-4f07-9d12-0fa54b78ab82"
    ),
    TradeSmithService("Gone Fishin' Portfolio", "29c07905-d56f-437a-ac1f-52a445789c68"),
    TradeSmithService(
        "The Oxford All-Star Portfolio", "666e1a6a-feb1-443c-82fb-3bd453f7fb0f"
    ),
    TradeSmithService(
        "Ten-Baggers of Tomorrow Portfolio", "ae436bca-86c5-4fb2-a7c1-56914b87b026"
    ),
    TradeSmithService("The Fortress Portfolio", "abbad8c3-b820-4312-bf01-ac0659911f8f"),
    TradeSmithService(
        "Profit Accelerator Portfolio", "6425da9f-504c-462d-b0a1-ba0beae8b061"
    ),
    TradeSmithService(
        "Instant Income Portfolio", "1c022877-a03c-4b1b-90cb-6480e284ec55"
    ),
    TradeSmithService(
        "Compound Income Portfolio", "0a9c022d-fce5-47b2-85be-27464df04bbf"
    ),
    TradeSmithService("High Yield Portfolio", "156378f4-947e-46d8-9e0f-5a5299311684"),
    TradeSmithService(
        "Fixed Income Funds - ETFs", "d60c5be6-a456-4482-8c54-42240c735545"
    ),
    TradeSmithService(
        "Income Accelerator Portfolio", "1b2732af-0592-4fcd-8dc7-283044730ddf"
    ),
    TradeSmithService(
        "The Insider Alert Portfolio", "f0dde727-02cb-4f72-a1c8-d002b8c1d42c"
    ),
    TradeSmithService(
        "Trigger Event Trader Portfolio", "f64563ef-fa48-4cb3-9d50-2dfa5fc0daa0"
    ),
    TradeSmithService(
        "The Momentum Alert Portfolio", "4c06afb4-3a31-44b5-a9e2-66c6aaf5d913"
    ),
    TradeSmithService(
        "Oxford Microcap Trader Portfolio", "e096ac62-833b-4b40-8fb4-aa36a606c4c7"
    ),
    TradeSmithService(
        "Technical Pattern Profits Portfolio", "81e7ec18-a343-4919-98b1-b579070a2886"
    ),
]

active_proxies: Set[str] = set()
alert_locks: Dict[str, asyncio.Lock] = {
    service.name: asyncio.Lock() for service in TRADESMITH_SERVICES
}
previous_alerts: Dict[str, List] = {}


def load_proxies():
    """Load proxies from json file"""
    try:
        with open(PROXY_FILE, "r") as f:
            data = json.load(f)
            return data["oxford_tradesmith"]
    except Exception as e:
        log_message(f"Error loading proxies: {e}", "ERROR")
        return []


async def get_available_proxy(proxies):
    """Get a random available proxy that isn't currently in use"""
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
    proxy = proxy
    active_proxies.discard(proxy)


def load_cookies(fresh=False) -> Optional[Dict]:
    try:
        cookies = None
        if not fresh:
            if not os.path.exists(SESSION_FILE):
                log_message(f"Session file not found: {SESSION_FILE}", "WARNING")
            else:
                with open(SESSION_FILE, "r") as f:
                    cookies = json.load(f)

        if not cookies or cookies.get("cf_clearance", "") == "":
            log_message(
                "Invalid or missing 'cf_clearance' in cookies. Attempting to regenerate.",
                "WARNING",
            )
            rand_guid = random.choice(TRADESMITH_SERVICES).guid
            bypass = bypasser(f"{BASE_URL}?guid={rand_guid}", SESSION_FILE)

            if not bypass or bypass is False:
                return None

            with open(SESSION_FILE, "r") as f:
                cookies = json.load(f)

            if not cookies or cookies.get("cf_clearance", "") == "":
                return None

        return cookies

    except json.JSONDecodeError:
        log_message("Failed to decode JSON from session file.", "ERROR")
    except Exception as e:
        log_message(f"Error loading session: {e}", "ERROR")

    return None


def get_service_file(service_name):
    """Get the file path for a specific service"""
    sanitized_name = service_name.lower().replace(" ", "_").replace("'", "")
    return os.path.join(DATA_DIR, f"{sanitized_name}.json")


def load_saved_data(service_name):
    """Load previously saved data for a specific service"""
    file_path = get_service_file(service_name)
    try:
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                return json.load(f)
        return []
    except Exception as e:
        log_message(f"Error loading saved data for {service_name}: {e}", "ERROR")
        return []


async def save_data(service_name, data):
    """Save data for a specific service"""
    async with alert_locks[service_name]:
        try:
            file_path = get_service_file(service_name)
            with open(file_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log_message(f"Error saving data for {service_name}: {e}", "ERROR")


def extract_data_rows(js_code):
    """Extract data rows from JavaScript code"""
    data_rows = []
    # Find all dataRows assignments using regex
    pattern = r"dataRows\[\d+\]=\{(.*?)\};"
    row_matches = re.findall(pattern, js_code, re.DOTALL)

    for row_content in row_matches:
        row_dict = {}
        # Split the row content by commas that are not inside quotes or HTML tags
        properties = []
        current_property = ""
        in_quotes = False
        in_html = False

        for char in row_content:
            if char == "'" and not in_html:
                in_quotes = not in_quotes
            elif char == "<":
                in_html = True
            elif char == ">":
                in_html = False

            if char == "," and not (in_quotes or in_html):
                properties.append(current_property.strip())
                current_property = ""
            else:
                current_property += char

        if current_property.strip():
            properties.append(current_property.strip())

        for prop in properties:
            parts = prop.split(":", 1)
            if len(parts) == 2:
                key = parts[0].strip().strip("'")
                value = parts[1].strip().strip("'")
                row_dict[key] = value

        data_rows.append(row_dict)

    return data_rows


def process_js_code(js_code, service):
    """Process JavaScript code to extract grid data"""
    try:
        if isinstance(js_code, str) and "dataRows" in js_code:
            data_rows = extract_data_rows(js_code)

            # Process the extracted rows to clean and standardize the data
            processed_rows = []
            for row in data_rows:
                processed_row = {}

                for key, value in row.items():
                    soup = BeautifulSoup(value, "html.parser")
                    clean_value = soup.get_text().strip()
                    processed_row[key] = clean_value

                    date_fields = ["Buy Date", "Entry Date"]
                    if key in date_fields:
                        url_match = re.search(r'href=\\"(.*?)\\"', value)
                        if url_match:
                            processed_row[f"url"] = url_match.group(1)

                    if key == "Current Price":
                        price_match = re.search(r'title=\\"(.*?)\\"', value)
                        if price_match:
                            processed_row[f"price"] = price_match.group(1)

                processed_rows.append(processed_row)

            return processed_rows
        return []
    except Exception as e:
        log_message(f"Error processing JS code for {service.name}: {e}", "ERROR")
        return []


def extract_changes(old_data, new_data):
    """Extract tickers that have been either added or removed"""
    try:
        old_symbols = {row.get("Symbol", "") for row in old_data if row.get("Symbol")}
        new_symbols = {row.get("Symbol", "") for row in new_data if row.get("Symbol")}

        # Find added and removed symbols
        added_symbols = new_symbols - old_symbols
        removed_symbols = old_symbols - new_symbols

        added_data = [row for row in new_data if row.get("Symbol", "") in added_symbols]

        removed_data = [
            row for row in old_data if row.get("Symbol", "") in removed_symbols
        ]

        return {"added": added_data, "removed": removed_data}
    except Exception as e:
        log_message(f"Error extracting changes: {e}", "ERROR")
        return {"added": [], "removed": []}


async def fetch_service_data(service, cookies, proxy):
    """Fetch data for a specific service using requests in an async-friendly way"""
    url = f"{BASE_URL}?guid={service.guid}"

    req_cookies = {"cf_clearance": cookies["cf_clearance"]}
    req_proxies = {"http": f"http://{proxy}"}
    headers = {
        "User-Agent": f"{cookies['user_agent']}",
        "Cache-Control": "max-age=0",
    }

    try:
        response = requests.get(
            url, proxies=req_proxies, headers=headers, cookies=req_cookies
        )
        await release_proxy(proxy)

        if response.status_code == 200:
            html_content = response.text

            # Check if the content contains prepareGrid function
            if "prepareGrid" not in html_content:
                log_message(
                    f"Content for {service.name} does not contain prepareGrid function",
                    "WARNING",
                )
                return None, None

            return html_content, None
        elif response.status_code == 403:
            log_message(
                f"CloudFlare error fetching {service.name}, attempting to refresh session",
                "WARNING",
            )
            cookies = load_cookies(fresh=True)
            if not cookies:
                raise Exception(
                    f"Failed to refresh CloudFlare session for {service.name}"
                )
            return None, cookies
        else:
            log_message(
                f"Failed to fetch {service.name}. Status: {response.status_code}",
                "ERROR",
            )
            return None, None
    except Exception as e:
        log_message(f"Error fetching {service.name} with requests: {e}", "ERROR")
        return None, None


def extract_grid_data(html_content, service):
    """Extract grid data from HTML content"""
    try:
        soup = BeautifulSoup(html_content, "html.parser")

        grid_id = service.guid.replace("-", "")
        function_name = f"prepareGridData{grid_id}"

        scripts = soup.find_all("script")
        for script in scripts:
            if script.string and f"function {function_name}" in script.string:
                js_code = script.string
                data_rows = process_js_code(js_code, service)
                return data_rows

        # If specific function not found, try to find any prepareGridData function
        for script in scripts:
            if script.string and "function prepareGridData" in script.string:
                js_code = script.string
                data_rows = process_js_code(js_code, service)
                return data_rows

        log_message(f"No grid data found for {service.name}", "WARNING")
        return []
    except Exception as e:
        log_message(f"Error extracting grid data for {service.name}: {e}", "ERROR")
        return []


async def process_service(service, cookies, proxy):
    """Process a single service and handle its data"""
    try:
        log_message(f"Processing {service.name} with proxy {proxy}", "INFO")

        html_content, new_cookies = await fetch_service_data(service, cookies, proxy)
        if new_cookies:
            return new_cookies

        if not html_content:
            return cookies

        grid_data = extract_grid_data(html_content, service)
        if not grid_data:
            log_message(f"No data extracted for {service.name}", "WARNING")
            return cookies

        previous_data = previous_alerts.get(service.name, [])
        changes = extract_changes(previous_data, grid_data)

        # If there are changes, send notifications
        if changes["added"] or changes["removed"]:
            current_time = get_current_time()

            added_text = ""
            for item in changes["added"]:
                url = item.get("url", "-")
                price = item.get("price", "-")
                symbol = item.get("Symbol", "-")

                added_text += f"- ADD: '{symbol}' at '{price}'. Article URL - {url}\n"

            removed_text = ""
            for item in changes["removed"]:
                url = item.get("url", "-")
                price = item.get("price", "-")
                symbol = item.get("Symbol", "-")

                removed_text += (
                    f"- REMOVE: '{symbol}' at '{price}'. Article URL - {url}\n\n"
                )

            changes_text = ""
            if added_text:
                changes_text += f"<b>Added:</b>\n{added_text}\n"
            if removed_text:
                changes_text += f"<b>Removed:</b>\n{removed_text}"

            message = (
                f"<b>TradeSmith Service Alert - {service.name}</b>\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"{changes_text}"
            )

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
            log_message(
                f"Sent alert for {service.name} with {len(changes['added'])} additions and {len(changes['removed'])} removals",
                "INFO",
            )

        previous_alerts[service.name] = grid_data
        await save_data(service.name, grid_data)

        return cookies
    except Exception as e:
        log_message(f"Error processing service {service.name}: {e}", "ERROR")
        return cookies


async def run_scraper():
    """Main scraper loop that respects market hours"""
    global previous_alerts

    for service in TRADESMITH_SERVICES:
        previous_alerts[service.name] = load_saved_data(service.name)

    proxies = load_proxies()
    if not proxies:
        log_message("No proxies available", "CRITICAL")
        return

    cookies = load_cookies()
    if not cookies:
        log_message("Failed to get valid cf_clearance", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting service monitoring...", "INFO")

        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()
            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...", "INFO")
                break

            log_message("Starting new scan cycle...", "INFO")
            start_time = time()

            for service in TRADESMITH_SERVICES:
                try:
                    proxy = await get_available_proxy(proxies)
                    cookies = await process_service(service, cookies, proxy)
                    if not cookies:
                        log_message(
                            "Lost cookies during processing, regenerating...",
                            "WARNING",
                        )
                        cookies = load_cookies(fresh=True)
                        if not cookies:
                            log_message(
                                "Failed to regenerate cookies, exiting scan cycle",
                                "ERROR",
                            )
                            break
                except Exception as e:
                    log_message(f"Error processing {service.name}: {e}", "ERROR")

            log_message(
                f"Scan cycle completed in {time() - start_time:.2f} seconds", "INFO"
            )
            await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP, WS_SERVER_URL]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        for service in TRADESMITH_SERVICES:
            if service.name in previous_alerts:
                asyncio.run(save_data(service.name, previous_alerts[service.name]))
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
