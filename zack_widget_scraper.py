import asyncio
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from time import time
from typing import List, Set

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
BATCH_SIZE = 250  # number of requests to run concurrently

DATA_DIR = Path("data")
CRED_DIR = Path("cred")
TICKERS_FILE = DATA_DIR / "zacks_tickers.json"
ALERTS_FILE = DATA_DIR / "zacks_widget_alerts.json"
PROXY_FILE = CRED_DIR / "zacks_proxies.json"

# Global variables
previous_alerts = set()
alert_lock = asyncio.Lock()
proxy_lock = asyncio.Lock()
active_proxies: Set[str] = set()


def load_proxies() -> List[str]:
    """Load proxies from json file"""
    try:
        with open(PROXY_FILE, "r") as f:
            data = json.load(f)
            return data["proxies"]
    except Exception as e:
        log_message(f"Error loading proxies: {e}", "ERROR")
        return []


async def get_available_proxy(proxies: List[str]) -> str:
    """Get a random available proxy that isn't currently in use"""
    async with proxy_lock:
        available_proxies = set(proxies) - active_proxies
        if not available_proxies:
            # If all proxies are in use, wait briefly and try again
            await asyncio.sleep(0.5)
            return await get_available_proxy(proxies)

        proxy = random.choice(list(available_proxies))
        active_proxies.add(proxy)
        return proxy


async def release_proxy(proxy: str):
    """Release a proxy back to the available pool"""
    async with proxy_lock:
        active_proxies.discard(proxy)


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


async def save_alerts(alerts):
    """Save alerts to disk with proper locking"""
    async with alert_lock:
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(ALERTS_FILE, "w") as f:
                json.dump(list(alerts), f, indent=2)
        except Exception as e:
            log_message(f"Error saving alerts: {e}", "ERROR")


async def fetch_ticker_data(session, ticker: str, proxy: str):
    """Fetch data for a single ticker using the provided proxy"""
    url = f"https://widget3.zacks.com/tradingservices/ticker_search/json/{ticker}"
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }
    proxy_http = f"http://{proxy}"

    try:
        async with session.get(url, headers=headers, proxy=proxy_http) as response:
            if response.status != 200:
                if response.status == 429:
                    log_message(
                        f"Rate limit hit for '{ticker}' using proxy {proxy}", "WARNING"
                    )
                    await asyncio.sleep(0.2)
                else:
                    log_message(
                        f"Failed to fetch '{ticker}' with proxy {proxy}. Status: {response.status}",
                        "ERROR",
                    )
                return ticker, None

            data = await response.json()
            return ticker, data
    except Exception as e:
        log_message(f"Error fetching {ticker} with proxy {proxy}: {e}", "ERROR")
        return ticker, None
    finally:
        await release_proxy(proxy)


async def process_batch(session, tickers: List[str], proxy: str):
    """Process a batch of tickers concurrently using available proxies"""
    tasks = [fetch_ticker_data(session, ticker, proxy) for ticker in tickers]
    return await asyncio.gather(*tasks)


async def process_results(results):
    """Process results and handle alerts"""
    global previous_alerts

    try:
        new_buys = set()
        new_sells = set()

        for ticker, data in results:
            if data is None:
                continue
            has_alert = isinstance(data, dict)

            if ticker in previous_alerts:
                if not has_alert:
                    new_sells.add(ticker)
            else:
                if has_alert:
                    new_buys.add(ticker)

        if new_buys or new_sells:
            current_time = datetime.now(pytz.utc)

            # # Send WebSocket messages
            # ws_tasks = []
            # for ticker in new_buys:
            #     ws_tasks.append(
            #         send_ws_message(
            #             {
            #                 "name": "Zacks Widget",
            #                 "type": "Buy",
            #                 "ticker": ticker,
            #                 "sender": "zacks",
            #             },
            #             WS_SERVER_URL,
            #         )
            #     )
            # for ticker in new_sells:
            #     ws_tasks.append(
            #         send_ws_message(
            #             {
            #                 "name": "Zacks Widget",
            #                 "type": "Sell",
            #                 "ticker": ticker,
            #                 "sender": "zacks_widget",
            #             },
            #             WS_SERVER_URL,
            #         )
            #     )
            #
            # await asyncio.gather(*ws_tasks)
            #
            # Prepare and send Telegram message
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

            # Update alerts with proper locking
            async with alert_lock:
                previous_alerts.difference_update(new_sells)
                previous_alerts.update(new_buys)
                await save_alerts(previous_alerts)

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
    proxies = load_proxies()

    if not tickers:
        log_message("No tickers loaded", "CRITICAL")
        return

    if not proxies:
        log_message("No proxies loaded", "CRITICAL")
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
                    batch_tasks = []

                    for i in range(0, len(tickers), BATCH_SIZE):
                        batch = tickers[i : i + BATCH_SIZE]
                        proxy = await get_available_proxy(proxies)

                        batch_tasks.append(process_batch(session, batch, proxy))

                    # Run all batches concurrently and gather results
                    all_batch_results = await asyncio.gather(*batch_tasks)

                    # Flatten results from all batches
                    all_results = []
                    for batch_result in all_batch_results:
                        all_results.extend(batch_result)

                    await process_results(all_results)

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
        asyncio.run(save_alerts(previous_alerts))
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
