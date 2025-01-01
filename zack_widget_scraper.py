import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from time import time

import aiohttp
import pytz
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("ZACKS_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("ZACKS_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
CHECK_INTERVAL = 0.2  # seconds between full list scans
BATCH_SIZE = 300  # number of requests to run concurrently
DATA_DIR = Path("data")
TICKERS_FILE = DATA_DIR / "zacks_tickers.json"
ALERTS_FILE = DATA_DIR / "zacks_widget_alerts.json"

# Global variables
previous_alerts = set()
alert_lock = asyncio.Lock()


def load_tickers():
    """Load tickers from json file"""
    try:
        with open(TICKERS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log_message(f"Error loading tickers: {e}", "ERROR")
        return []


def load_saved_alerts():
    """Load previously saved alerts from disk"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        if ALERTS_FILE.exists():
            with open(ALERTS_FILE, "r") as f:
                return set(json.load(f))
        return set()
    except Exception as e:
        log_message(f"Error loading saved alerts: {e}", "ERROR")
        return set()


def save_alerts(alerts):
    """Save alerts to disk"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(ALERTS_FILE, "w") as f:
            json.dump(list(alerts), f, indent=2)
    except Exception as e:
        log_message(f"Error saving alerts: {e}", "ERROR")


async def fetch_ticker_data(session, ticker):
    """Fetch data for a single ticker"""
    url = f"https://widget3.zacks.com/tradingservices/ticker_search/json/{ticker}"
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    try:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                return ticker, None

            data = await response.json()
            return ticker, data
    except Exception as e:
        log_message(f"Error fetching {ticker}: {e}", "ERROR")
        return ticker, None


async def process_batch(session, tickers):
    """Process a batch of tickers concurrently"""
    tasks = [fetch_ticker_data(session, ticker) for ticker in tickers]
    return await asyncio.gather(*tasks)


async def process_results_old(results):
    """Process results and handle alerts"""
    global previous_alerts

    try:
        current_alerts = set()
        new_buys = set()
        new_sells = set()

        # Process results to find current alerts
        for ticker, data in results:
            if data and len(data) > 0:  # Ticker has active alerts
                current_alerts.add(ticker)

        async with alert_lock:
            # Find new buys (tickers that weren't in previous alerts but are now)
            new_buys = current_alerts - previous_alerts
            # Find new sells (tickers that were in previous alerts but aren't now)
            new_sells = previous_alerts - current_alerts

            if new_buys or new_sells:
                current_time = datetime.now(pytz.utc)

                for ticker in new_buys:
                    await send_ws_message(
                        {
                            "name": "Zacks Widget",
                            "type": "Buy",
                            "ticker": ticker,
                            "sender": "zacks",
                        },
                        WS_SERVER_URL,
                    )

                for ticker in new_sells:
                    await send_ws_message(
                        {
                            "name": "Zacks Widget",
                            "type": "Sell",
                            "ticker": ticker,
                            "sender": "zacks_widget",
                        },
                        WS_SERVER_URL,
                    )

                changes = []
                if new_buys:
                    changes.append(
                        f"New Buys:\n" + "\n".join(f"- {t}" for t in new_buys)
                    )
                if new_sells:
                    changes.append(
                        f"New Sells:\n" + "\n".join(f"- {t}" for t in new_sells)
                    )

                message = (
                    f"<b>Zacks Widget Alert!</b>\n"
                    f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                    f"\n{'\n'.join(changes)}"
                )

                await send_telegram_message(
                    message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                )

                # Update previous alerts and save to disk
                previous_alerts = current_alerts
                save_alerts(previous_alerts)

                log_message(
                    f"Processed changes - Buys: {len(new_buys)}, Sells: {len(new_sells)}"
                )

    except Exception as e:
        log_message(f"Error processing results: {e}", "ERROR")


async def process_results(results):
    """Process results and handle alerts"""
    global previous_alerts

    try:
        new_buys = set()
        new_sells = set()

        for ticker, data in results:
            # Check if data is an object (dictionary) vs empty (None or [])
            has_alert = isinstance(data, dict)

            # If ticker is in our saved list
            if ticker in previous_alerts:
                # If no data returned, it's a sell
                if not has_alert:
                    new_sells.add(ticker)
            else:
                # If ticker not in our list and has data, it's a buy
                if has_alert:
                    new_buys.add(ticker)

        if new_buys or new_sells:
            current_time = datetime.now(pytz.utc)

            for ticker in new_buys:
                await send_ws_message(
                    {
                        "name": "Zacks Widget",
                        "type": "Buy",
                        "ticker": ticker,
                        "sender": "zacks",
                    },
                    WS_SERVER_URL,
                )

            for ticker in new_sells:
                await send_ws_message(
                    {
                        "name": "Zacks Widget",
                        "type": "Sell",
                        "ticker": ticker,
                        "sender": "zacks_widget",
                    },
                    WS_SERVER_URL,
                )

            changes = []
            if new_buys:
                changes.append(f"New Buys:\n" + "\n".join(f"- {t}" for t in new_buys))
            if new_sells:
                changes.append(f"New Sells:\n" + "\n".join(f"- {t}" for t in new_sells))

            message = (
                f"<b>Zacks Widget Alert!</b>\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                f"\n{'\n'.join(changes)}"
            )

            await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

            # Update the master list - remove sells and add buys
            previous_alerts.difference_update(new_sells)
            previous_alerts.update(new_buys)

            # Save the updated master list
            save_alerts(previous_alerts)

            log_message(
                f"Processed changes - Buys: {len(new_buys)}, Sells: {len(new_sells)}"
            )

    except Exception as e:
        log_message(f"Error processing results: {e}", "ERROR")


async def run_scraper():
    """Main scraper loop that respects market hours"""
    global previous_alerts

    previous_alerts = load_saved_alerts()
    tickers = load_tickers()

    if not tickers:
        log_message("No tickers loaded", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting ticker scanning...")

        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))
            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...")
                break

            start_time = time()
            log_message("Starting new scan cycle...")

            try:
                async with aiohttp.ClientSession() as session:
                    # Process tickers in batches
                    for i in range(0, len(tickers), BATCH_SIZE):
                        batch = tickers[i : i + BATCH_SIZE]
                        results = await process_batch(session, batch)
                        await process_results(results)

                        # TODO: Small delay between batches - remove after adding proxies
                        await asyncio.sleep(0.2)

                log_message(
                    f"Scan cycle completed in {time() - start_time:.2f} seconds"
                )
                await asyncio.sleep(CHECK_INTERVAL)

            except Exception as e:
                log_message(f"Error in scraper loop: {e}", "ERROR")
                await asyncio.sleep(1)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WS_SERVER_URL]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        save_alerts(previous_alerts)
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
