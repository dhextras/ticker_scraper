import asyncio
import json
import math
import os
import random
import sys
from pathlib import Path
from time import time
from typing import List, Set

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
TELEGRAM_BOT_TOKEN = os.getenv("ZACKS_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("ZACKS_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
BATCH_SIZE = 200  # number of requests to run concurrently

DATA_DIR = Path("data")
CRED_DIR = Path("cred")
TICKERS_FILE = DATA_DIR / "zacks_tickers.json"
ALERTS_FILE = DATA_DIR / "zacks_widget_alerts.json"
PROXY_FILE = CRED_DIR / "proxies.json"

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
            return data["zacks_widget"]
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
    url = f"https://zacks.com/tradingservices/ts_ajax_data_handler.php"
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    params = {"module_type": "ticker_search", "t": ticker, "get_param": "value"}

    try:
        async with session.get(url, headers=headers, params=params) as response:
            if response.status != 200:
                if response.status == 429:
                    log_message(
                        f"Rate limit hit for '{ticker}' using proxy {proxy}", "WARNING"
                    )
                    await asyncio.sleep(60)
                elif 500 <= response.status < 600:
                    log_message(
                        f"Server error {response.status}: Temporary issue, safe to ignore if infrequent."
                        "WARNING",
                    )
                else:
                    log_message(
                        f"Failed to fetch '{ticker}' with proxy {proxy}. Status: {response.status}",
                        "ERROR",
                    )
                return ticker, None

            response_text = await response.text()

            try:
                if "Zacks Investment Research" in response_text:
                    log_message(
                        f"Failed to get data and hit with the home page for '{ticker}'",
                        "WARNING",
                    )
                    return ticker, None
                return ticker, json.loads(response_text)
            except:
                log_message(
                    f"Failed to convert the response to json for '{ticker}' with proxy {proxy}",
                    "WARNING",
                )
                return ticker, None

    except Exception as e:
        log_message(f"Error fetching {ticker} with proxy {proxy}: {e}", "ERROR")
        return ticker, None


async def process_batch(tickers: List[str], proxy: str):
    """Process a batch of tickers concurrently using available proxies"""
    proxy_url = f"http://{proxy}"

    # Create a new session with the proxy already configured
    connector = aiohttp.TCPConnector(force_close=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        session._default_proxy = proxy_url

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

            if ticker in previous_alerts and not has_alert:
                new_sells.add(ticker)
            elif ticker not in previous_alerts and has_alert:
                new_buys.add(ticker)

        if new_buys or new_sells:
            current_time = get_current_time()

            # Send WebSocket messages
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
            #                 "sender": "zacks",
            #             },
            #             WS_SERVER_URL,
            #         )
            #     )
            #
            # await asyncio.gather(*ws_tasks)

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

            # Update alerts first
            previous_alerts.difference_update(new_sells)
            previous_alerts.update(new_buys)

            # Run operations concurrently
            await asyncio.gather(
                send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID),
                save_alerts(previous_alerts),
            )

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

    if not tickers or not proxies:
        log_message("Missing tickers or proxies", "CRITICAL")
        return

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting ticker scanning...", "DEBUG")

        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()
            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                break

            start_time = time()
            log_message("Starting new scan cycle...")

            try:
                all_results = []
                # tasks = []

                # NOTE: Run Every batch one by one to avoid getting hit with the home page
                for i in range(0, len(tickers), BATCH_SIZE):
                    start_time_2 = time()
                    batch = tickers[i : i + BATCH_SIZE]
                    proxy = await get_available_proxy(proxies)

                    # Process the batch sequentially
                    batch_result = await process_batch(batch, proxy)
                    all_results.extend(batch_result)
                    log_message(
                        f"fetched batch {i//BATCH_SIZE + 1}/{math.ceil(len(tickers)/BATCH_SIZE)} in {(time()- start_time_2):2f} with proxy {proxy}",
                        "INFO",
                    )

                    await release_proxy(proxy)
                    await process_results(batch_result)

                # NOTE: Create all tasks at once - use it if provne usefull
                # for i in range(0, len(tickers), BATCH_SIZE):
                #     batch = tickers[i : i + BATCH_SIZE]
                #
                #     tasks.append(process_batch(batch, proxy))
                #
                # # Run all batches truly concurrently
                # batch_results = await asyncio.gather(*tasks)
                #
                # # Flatten results
                # for batch in batch_results:
                #     all_results.extend(batch)
                #
                log_message(
                    f"Scan cycle completed in {time() - start_time:.2f} seconds"
                )
                await asyncio.sleep(1)

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
