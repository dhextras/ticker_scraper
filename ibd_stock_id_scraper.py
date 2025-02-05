import asyncio
import json
import os
import sys
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
CHECK_INTERVAL = 1
DEFAULT_STARTING_ID = 2400  # Default starting ID - change it later if needed
LAST_ID_FILE = "data/ibd_last_processed_id.json"
CRED_FILE = "cred/ibd_creds.json"
TELEGRAM_BOT_TOKEN = os.getenv("IBD_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("IBD_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

os.makedirs("data", exist_ok=True)


def load_creds():
    try:
        with open(CRED_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log_message(f"Error loading credentials: {e}", "ERROR")
        return None


def load_last_id():
    try:
        with open(LAST_ID_FILE, "r") as f:
            data = json.load(f)
            return data.get("last_id", DEFAULT_STARTING_ID) + 1
    except FileNotFoundError:
        return DEFAULT_STARTING_ID + 1


def save_last_id(last_id):
    with open(LAST_ID_FILE, "w") as f:
        json.dump({"last_id": last_id}, f, indent=2)
    log_message(f"Last processed ID saved: {last_id}", "INFO")


def get_cookies(creds):
    return {
        ".ASPXAUTH": creds["auth_token"],
        "ibdSession": f"Webuserid={creds['user_id']}&RolesUpdated=True&LogInFlag=1&SessionId={creds['session_id']}",
    }


async def fetch_trade(session, creds, trade_id):
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://swingtrader.investors.com",
        "referer": "https://swingtrader.investors.com/?ibdsilentlogin=true",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "webuserid": creds["user_id"],
    }

    try:
        async with session.get(
            f"https://swingtrader.investors.com/api/trade/stock?id={trade_id}",
            cookies=get_cookies(creds),
            headers=headers,
        ) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 404:
                return None
            elif response.status == 429:
                log_message(f"Too Many requests, slow down...", "ERROR")
                await asyncio.sleep(CHECK_INTERVAL * 5)
                return None
            log_message(f"Failed to fetch trade: HTTP {response.status}", "ERROR")
            return None
    except Exception as e:
        log_message(f"Error fetching trade: {e}", "ERROR")
        return None


async def send_alerts(trade):
    await send_ws_message(
        {
            "name": "IBD SwingTrader - Id",
            "type": "Buy",
            "ticker": trade["stockSymbol"],
            "sender": "ibd_swing",
            "target": "CSS",
        },
        WS_SERVER_URL,
    )

    current_time = datetime.now(pytz.timezone("US/Eastern"))
    created_time = datetime.fromisoformat(trade["created"].replace("Z", "+00:00"))

    message = f"<b>New IBD SwingTrader Alert!</b>\n\n"
    message += f"<b>ID:</b> {trade['id']}\n"
    message += f"<b>Symbol:</b> {trade['stockSymbol']}\n"
    message += f"<b>Company:</b> {trade['companyName']}\n"
    message += f"<b>Created:</b> {created_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"Trade alert sent to Websocket & Telegram: {trade['stockSymbol']}", "INFO"
    )


async def run_scraper():
    creds = load_creds()
    if not creds:
        log_message("Failed to load credentials", "CRITICAL")
        return

    current_id = load_last_id()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            log_message("Market is open. Starting to check for new trades...")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = datetime.now(pytz.timezone("America/New_York"))
                if current_time > market_close_time:
                    log_message("Market is closed. Waiting for next market open...")
                    break

                trade_data = await fetch_trade(session, creds, current_id)

                if trade_data:
                    await send_alerts(trade_data)
                    save_last_id(current_id)
                    current_id += 1
                    log_message(f"Moving to next ID: {current_id}", "INFO")
                else:
                    log_message(f"No trade found for ID: {current_id}", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
