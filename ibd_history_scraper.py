import asyncio
import json
import os
import sys
from datetime import datetime

import aiohttp
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
CHECK_INTERVAL = 2
PROCESSED_TRADES_FILE = "data/ibd_processed_history_trades.json"
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


def load_processed_trades():
    try:
        with open(PROCESSED_TRADES_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_trades(trades):
    with open(PROCESSED_TRADES_FILE, "w") as f:
        json.dump(list(trades), f, indent=2)
    log_message("Processed trades saved.", "INFO")


def get_cookies(creds):
    return {
        ".ASPXAUTH": creds["auth_token"],
        "ibdSession": f"Webuserid={creds['user_id']}&RolesUpdated=True&LogInFlag=1&SessionId={creds['session_id']}",
    }


async def fetch_trades(session, creds):
    params = {
        "state": "CURRENT",
        "pageSize": "20",
    }

    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://swingtrader.investors.com",
        "referer": "https://swingtrader.investors.com/?ibdsilentlogin=true",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "webuserid": creds["user_id"],
    }

    try:
        async with session.get(
            "https://swingtrader.investors.com/api/trade/year/0",
            params=params,
            cookies=get_cookies(creds),
            headers=headers,
        ) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 429:
                log_message(f"Too Many requests, slow down...", "ERROR")
                await asyncio.sleep(CHECK_INTERVAL * 5)
                return None
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return None
            log_message(f"Failed to fetch trades: HTTP {response.status}", "ERROR")
            return None
    except Exception as e:
        log_message(f"Error fetching trades: {e}", "ERROR")
        return None


async def send_to_telegram(trade):
    # await send_ws_message(
    #     {
    #         "name": "IBD SwingTrader - H",
    #         "type": "Buy",
    #         "ticker": trade["stockSymbol"],
    #         "sender": "ibd_swing",
    #         "target": "CSS",
    #     },
    #     WS_SERVER_URL,
    # )

    current_time = get_current_time()
    created_time = datetime.fromisoformat(trade["created"].replace("Z", "+00:00"))

    message = f"<b>New IBD SwingTrader History Alert!</b>\n\n"
    message += f"<b>ID:</b> {trade['id']}\n"
    message += f"<b>Symbol:</b> {trade['stockSymbol']}\n"
    message += f"<b>Company:</b> {trade['companyName']}\n"
    message += f"<b>Created:</b> {created_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"Trade alert sent to Telegram & Websocket: {trade['stockSymbol']}", "INFO"
    )


async def run_scraper():
    creds = load_creds()
    if not creds:
        log_message("Failed to load credentials", "CRITICAL")
        return

    processed_trades = load_processed_trades()

    async with aiohttp.ClientSession() as session:

        while True:
            await sleep_until_market_open(start=8, end=15)
            log_message("Market is open. Starting to check for new trades...", "DEBUG")
            _, _, market_close_time = get_next_market_times(start=8, end=15)

            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                trades_data = await fetch_trades(session, creds)
                if trades_data:
                    new_trades = [
                        trade
                        for trade in trades_data["trades"]
                        if str(trade["id"]) not in processed_trades
                    ]

                    if new_trades:
                        for trade in new_trades:
                            await send_to_telegram(trade)
                            processed_trades.add(str(trade["id"]))
                        save_processed_trades(processed_trades)
                    else:
                        log_message("No new trades found.", "INFO")

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
