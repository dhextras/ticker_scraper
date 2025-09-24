import asyncio
import json
import os
import random
import time
import uuid
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv

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
PORTFOLIO_API_URL = "https://my.paradigmpressgroup.com/api/portfolio"

# NOTE: These are for mm2 and its one trading group, check for the group name in the json to find the id
PORTFOLIO_ID = "14531"
TARGET_TRADE_GROUP_ID = "rectTwpnhShFaZLu2"

# NOTE : For mvk
# PORTFOLIO_ID = "14557"
# TARGET_TRADE_GROUP_ID = "recoVwjygN2ckIIeV"

service_name = "mm2"

CHECK_INTERVAL = 1.0
PROCESSED_POSITIONS_FILE = f"data/{service_name}_processed_positions.json"
TELEGRAM_BOT_TOKEN = os.getenv("ALTUCHER_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("ALTUCHER_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)


def load_processed_positions():
    try:
        with open(PROCESSED_POSITIONS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_positions(positions):
    with open(PROCESSED_POSITIONS_FILE, "w") as f:
        json.dump(list(positions), f, indent=2)
    log_message("Processed positions saved.", "INFO")


def get_cache_buster():
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

    return random.choice(cache_busters)


async def fetch_portfolio_data(session):
    try:
        params = {"id": PORTFOLIO_ID, "_": get_cache_buster()}

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "DNT": "1",
            "Host": "my.paradigmpressgroup.com",
            "Pragma": "no-cache",
            "Referer": f"https://my.paradigmpressgroup.com/subscription/{service_name}/portfolio",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
        }

        start_time = time.time()
        async with session.get(
            PORTFOLIO_API_URL, params=params, headers=headers, timeout=10
        ) as response:
            if response.status == 200:
                data = await response.json()
                log_message(
                    f"Fetched portfolio data, took {(time.time() - start_time):.2f}s",
                    "INFO",
                )
                return data
            else:
                log_message(
                    f"Failed to fetch portfolio: HTTP {response.status}",
                    "ERROR",
                )
                return []
    except asyncio.TimeoutError:
        log_message("Timeout fetching portfolio data", "WARNING")
        return []
    except Exception as e:
        log_message(f"Error fetching portfolio: {e}", "ERROR")
        return []


def process_portfolio_positions(data):
    filtered_positions = []

    for position in data:
        trade_group_id = position.get("TradeGroupId")

        if trade_group_id == TARGET_TRADE_GROUP_ID:
            position_data = {
                "name": position.get("Name", ""),
                "symbol": position.get("Symbol", ""),
                "open_datetime": position.get("OpenDateTime", ""),
                "open_date_link": position.get("PositionSetting", {}).get(
                    "OpenDateLink", ""
                ),
                "trade_group_id": trade_group_id,
                "position_id": position.get("Id", ""),
            }
            filtered_positions.append(position_data)

    return filtered_positions


def format_datetime(datetime_str):
    if not datetime_str:
        return "N/A"

    try:
        dt = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
        us_time = dt.astimezone(pytz.timezone("America/Chicago"))
        return us_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    except:
        return datetime_str


async def send_new_positions_to_telegram(new_positions):
    current_time_formatted = get_current_time().strftime("%Y-%m-%d %H:%M:%S %Z")

    for position in new_positions:
        symbol = position["symbol"]
        name = position["name"]
        open_datetime = format_datetime(position["open_datetime"])
        open_date_link = position["open_date_link"]

        message = f"<b>New {service_name.upper()} Portfolio Position</b>\n\n"
        message += f"<b>Symbol:</b> {symbol}\n"
        message += f"<b>Name:</b> {name}\n"
        message += f"<b>Open Time:</b> {open_datetime}\n"
        if open_date_link:
            message += f"<b>Article URL:</b> {open_date_link}\n"
        message += f"<b>Current Time:</b> {current_time_formatted}\n"

        await send_ws_message(
            {
                "name": f"Altucher Portfolio - {service_name.upper()}",
                "type": "Buy",
                "ticker": symbol,
                "sender": "altucher",
            }
        )

        await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)

        log_message(
            f"New position sent: {symbol} - {name}",
            "INFO",
        )


async def run_monitor():
    processed_positions = load_processed_positions()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message("Market is open. Starting portfolio monitoring...", "DEBUG")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                portfolio_data = await fetch_portfolio_data(session)

                if portfolio_data:
                    filtered_positions = process_portfolio_positions(portfolio_data)

                    new_positions = []
                    current_position_ids = set()

                    for position in filtered_positions:
                        position_id = position["position_id"]
                        current_position_ids.add(position_id)

                        if position_id not in processed_positions:
                            new_positions.append(position)

                    if new_positions:
                        log_message(f"Found {len(new_positions)} new positions", "INFO")
                        await send_new_positions_to_telegram(new_positions)
                        processed_positions.update(current_position_ids)
                        save_processed_positions(processed_positions)
                    else:
                        log_message("No new positions found", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        return

    try:
        asyncio.run(run_monitor())
    except KeyboardInterrupt:
        log_message("Monitor shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in monitor: {e}", "CRITICAL")


if __name__ == "__main__":
    main()
