import asyncio
import json
import os
import re
import sys
from typing import Dict, Optional, Tuple

import requests
import websockets
from bs4 import BeautifulSoup
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

LOGIN_URL = "https://oxfordclub.com/wp-login.php"
USERNAME = os.getenv("OXFORDCLUB_USERNAME")
PASSWORD = os.getenv("OXFORDCLUB_PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("OXFORDCLUB_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("OXFORDCLUB_TELEGRAM_GRP")
WEBSOCKET_PORT = 8766

connected_websockets = set()
session = None


def login_sync(session: requests.Session) -> bool:
    try:
        payload = {"log": USERNAME, "pwd": PASSWORD}
        response = session.post(LOGIN_URL, data=payload)
        if response.status_code == 200:
            log_message("Login successful", "INFO")
            return True
        else:
            log_message(f"Login failed: HTTP {response.status_code}", "ERROR")
            return False
    except Exception as e:
        log_message(f"Error during login: {e}", "ERROR")
        return False


def get_headers() -> Dict[str, str]:
    return {
        "Connection": "keep-alive",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36",
    }


def parse_message(message: str) -> Optional[Tuple[str, str, str]]:
    pattern = r"The Oxford Club:\s+(.+?)\s+\[(.+?)\]\s+(https?://[^\s]+)"
    match = re.search(pattern, message, re.IGNORECASE)

    if match:
        service_name = match.group(1).strip()
        sentiment = match.group(2).strip().lower()
        url = match.group(3).strip()
        return service_name, sentiment, url

    return None


async def process_page(
    session: requests.Session, url: str
) -> Optional[Tuple[str, str]]:
    try:
        response = await asyncio.to_thread(
            session.get, url, headers=get_headers(), timeout=15
        )

        if response.status_code == 200:
            content = response.text
            soup = BeautifulSoup(content, "html.parser")
            all_text = soup.get_text(separator=" ", strip=True)

            action_sections = re.split(r"Action to Take", all_text, flags=re.IGNORECASE)

            if len(action_sections) < 2:
                log_message(f"'Action to Take' not found: {url}", "WARNING")
                return None

            for section in action_sections[1:]:
                buy_match = re.search(r"Buy", section, re.IGNORECASE)
                sell_match = re.search(r"Sell", section, re.IGNORECASE)
                ticker_match = re.search(
                    r"(?:NYSE|NASDAQ)\s*:\s*\(?\*?([A-Z]{1,5})\*?\)?",
                    section,
                    re.IGNORECASE,
                )

                ticker: str = ""

                if ticker_match:
                    ticker = ticker_match.group(1)
                else:
                    ticker_match = re.search(
                        r"\(\s*([A-Z]{1,5})\s*\)",
                        section,
                        re.IGNORECASE,
                    )
                    if ticker_match:
                        ticker = ticker_match.group(1)

                if ticker:
                    if (
                        sell_match
                        and ticker_match
                        and sell_match.start() < ticker_match.start()
                    ):
                        ticker = ticker_match.group(1)
                        return ticker, "Sell"

                    elif (
                        buy_match
                        and ticker_match
                        and buy_match.start() < ticker_match.start()
                    ):
                        ticker = ticker_match.group(1)
                        return ticker, "Buy"

            log_message(f"No ticker found in URL: {url}", "WARNING")
        else:
            log_message(f"Failed to fetch page: HTTP {response.status_code}", "ERROR")
    except Exception as e:
        log_message(f"Error processing page {url}: {e}", "ERROR")

    return None


async def process_sms_message(
    session: requests.Session, message: str, message_timestamp: str
):
    current_time = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")

    parsed = parse_message(message)
    if not parsed:
        telegram_message = f"<b>Oxford Club SMS - Invalid Format</b>\n\n"
        telegram_message += f"<b>Message:</b> {message[:200]}...\n"
        telegram_message += f"<b>Message Time:</b> {message_timestamp}\n"
        telegram_message += f"<b>Current Time:</b> {current_time}"

        await send_telegram_message(telegram_message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
        return

    service_name, sentiment, url = parsed
    result = await process_page(session, url)

    telegram_message = f"<b>Oxford Club SMS - {service_name}</b>\n\n"

    if result:
        ticker, action = result

        await send_ws_message(
            {
                "name": "Oxford Club SMS",
                "type": (
                    action if sentiment not in ["buy", "sell"] else sentiment.title()
                ),
                "ticker": ticker,
                "sender": "oxfordclub",
            }
        )

        telegram_message += f"<b>Action:</b> {action}\n"
        telegram_message += f"<b>Ticker:</b> {ticker}\n"

    telegram_message += f"<b>URL:</b> {url}\n"
    telegram_message += f"<b>Message Time:</b> {message_timestamp}\n"
    telegram_message += f"<b>Current Time:</b> {current_time}"

    await send_telegram_message(telegram_message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def websocket_ping_loop():
    while True:
        await asyncio.sleep(10)

        if connected_websockets:
            log_message(
                f"[WebSocket] Sending ping to {len(connected_websockets)} connected clients",
                "INFO",
            )

            clients_to_ping = connected_websockets.copy()

            for websocket in clients_to_ping:
                try:
                    await websocket.send(json.dumps({"dt": "ping"}))
                except websockets.exceptions.ConnectionClosed:
                    log_message(
                        "[WebSocket] Client disconnected during ping", "WARNING"
                    )
                    connected_websockets.discard(websocket)
                except Exception as e:
                    log_message(
                        f"[WebSocket] Error sending ping to client: {e}", "WARNING"
                    )
                    connected_websockets.discard(websocket)


async def handle_websocket_message(websocket):
    global session

    if not session:
        session = requests.Session()

        if not login_sync(session):
            log_message("Failed to login to Oxford Club", "ERROR")
            return

    connected_websockets.add(websocket)
    client_address = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
    log_message(f"[WebSocket] New client connected: {client_address}", "INFO")

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                dtype = data.get("dt", "")

                if dtype == "pong":
                    continue

                if dtype == "s":
                    sms_message = data.get("m", "")
                    message_timestamp = data.get("t", "")

                    if sms_message:
                        log_message(f"Received SMS: {sms_message[:100]}...", "INFO")
                        await process_sms_message(
                            session, sms_message, message_timestamp
                        )
                    else:
                        log_message("Received empty SMS message", "WARNING")

            except json.JSONDecodeError:
                log_message("Received invalid JSON", "WARNING")
            except Exception as e:
                log_message(f"Error processing WebSocket message: {e}", "ERROR")

    except websockets.exceptions.ConnectionClosed:
        log_message(f"[WebSocket] Client {client_address} disconnected", "INFO")
    except Exception as e:
        log_message(f"[WebSocket] Error handling client {client_address}: {e}", "ERROR")
    finally:
        connected_websockets.discard(websocket)
        log_message(f"[WebSocket] Client {client_address} removed", "INFO")


async def start_websocket_server():
    log_message(f"Starting WebSocket server on port {WEBSOCKET_PORT}", "INFO")
    return await websockets.serve(handle_websocket_message, "0.0.0.0", WEBSOCKET_PORT)


async def run_server():
    global session

    session = requests.Session()
    if not login_sync(session):
        log_message("Failed to login to Oxford Club", "ERROR")
        return

    while True:
        await sleep_until_market_open()
        await initialize_websocket()

        log_message("Market is open. Starting Oxford Club SMS server...", "DEBUG")
        _, _, market_close_time = get_next_market_times()

        websocket_server = await start_websocket_server()
        ping_task = asyncio.create_task(websocket_ping_loop())

        try:
            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message("Market is closed. Stopping server...", "DEBUG")
                    break

                await asyncio.sleep(60)

        finally:
            ping_task.cancel()
            websocket_server.close()
            await websocket_server.wait_closed()


async def main_async():
    try:
        await run_server()
    except Exception as e:
        log_message(f"Error in main async: {e}", "CRITICAL")


def main():
    if not all([USERNAME, PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
