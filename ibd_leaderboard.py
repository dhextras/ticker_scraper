import asyncio
import json
import os
import sys

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
CHECK_INTERVAL = 0.5
PROCESSED_ALERTS_FILE = "data/ibd_processed_leaderboard_alerts.json"
CRED_FILE = "cred/ibd_creds.json"
TELEGRAM_BOT_TOKEN = os.getenv("IBD_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("IBD_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

os.makedirs("data", exist_ok=True)


def get_trade_type(ar_flag):
    if ar_flag == 0:
        return "Sell"
    elif ar_flag == 1:
        return "Buy"
    elif ar_flag == 2:
        return (
            "Sell"  # FIXME: Find out if its gonna be a sell or buy (Trim - Unconfirmed)
        )
    return "Buy"


def load_creds():
    try:
        with open(CRED_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log_message(f"Error loading credentials: {e}", "ERROR")
        return None


def load_processed_alerts():
    try:
        with open(PROCESSED_ALERTS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_alerts(alerts):
    with open(PROCESSED_ALERTS_FILE, "w") as f:
        json.dump(list(alerts), f, indent=2)
    log_message("Processed alerts saved.", "INFO")


def get_cookies(creds):
    return {
        ".ASPXAUTH": creds["auth_token"],
        "ibdSession": f"Webuserid={creds['user_id']}&RolesUpdated=True&LogInFlag=1&SessionId={creds['session_id']}",
    }


async def fetch_alerts(session, creds):
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "referer": "https://leaderboard.investors.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    }

    try:
        async with session.get(
            "https://leaderboard.investors.com/alertapi/alerts/recentactions",
            cookies=get_cookies(creds),
            headers=headers,
        ) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("Obj")
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
            log_message(f"Failed to fetch alerts: HTTP {response.status}", "ERROR")
            return None
    except Exception as e:
        log_message(f"Error fetching alerts: {e}", "ERROR")
        return None


async def send_to_telegram(alert):
    trade_type = get_trade_type(alert.get("ARFlag"))

    # await send_ws_message(
    #     {
    #         "name": "IBD Leaderboard - RA",
    #         "type": trade_type,
    #         "ticker": alert["Symbol"],
    #         "sender": "ibd_leaderboard",
    #         "target": "CSS",
    #     },
    #     WS_SERVER_URL,
    # )

    current_time = get_current_time()

    message = f"<b>New IBD Leaderboard Recent actions!</b>\n\n"
    message += f"<b>Id:</b> {alert['Id']}\n"
    message += f"<b>Symbol:</b> {alert['Symbol']}\n"
    message += f"<b>Company:</b> {alert['CoName']}\n"
    message += f"<b>Action:</b> {trade_type}\n"
    message += f"<b>ARFlag:</b> {alert.get('ARFlag')}\n"
    message += f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"Alert sent to Telegram & Websocket: {alert['Symbol']} - {trade_type}", "INFO"
    )


async def run_scraper():
    creds = load_creds()
    if not creds:
        log_message("Failed to load credentials", "CRITICAL")
        return

    processed_alerts = load_processed_alerts()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open(start=8, end=15)
            log_message("Market is open. Starting to check for new alerts...", "DEBUG")
            _, _, market_close_time = get_next_market_times(start=8, end=15)

            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                alerts_data = await fetch_alerts(session, creds)
                if alerts_data:
                    new_alerts = [
                        alert
                        for alert in alerts_data
                        if str(alert["Id"]) not in processed_alerts
                    ]

                    if new_alerts:
                        for alert in new_alerts:
                            await send_to_telegram(alert)
                            processed_alerts.add(str(alert["Id"]))
                        save_processed_alerts(processed_alerts)
                    else:
                        log_message("No new alerts found.", "INFO")

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
