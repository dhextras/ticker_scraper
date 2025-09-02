import asyncio
import json
import os
import sys

import aiohttp
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.ticker_deck_sender import initialize_ticker_deck, send_ticker_deck_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()

# Constants
CREDENTIALS_FILE = "cred/godel_token.json"
CHAT_CHANNEL_ID = "c9a4976e-8b94-4dd4-be83-dd641a084589"
TARGET_USERNAME = "martin"

TELEGRAM_BOT_TOKEN = os.getenv("GODEL_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("GODEL_TELEGRAM_GRP")

os.makedirs("cred", exist_ok=True)


def load_jwt_token():
    try:
        with open(CREDENTIALS_FILE, "r") as f:
            data = json.load(f)
            return data.get("jwt_token", "")
    except FileNotFoundError:
        log_message(
            "JWT token file not found. Please add token to cred/godel_token.json",
            "ERROR",
        )
        sys.exit(1)
    except Exception as e:
        log_message(f"Error loading JWT token: {e}", "ERROR")
        sys.exit(1)


async def send_alert(msg: str):
    alert = f"🚨 GODEL ALERT: {msg}\nPlease check the server immediately!"
    if TELEGRAM_BOT_TOKEN and TELEGRAM_GRP:
        await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"ALERT: {msg}", "CRITICAL")


class GodelChatMonitor:
    def __init__(self):
        self.session = None
        self.ws = None
        self.jwt_token = load_jwt_token()
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5

    def format_stomp_message(self, command, headers=None, body=None):
        message = command + "\n"
        if headers:
            for key, value in headers.items():
                message += f"{key}:{value}\n"
        message += "\n"
        if body:
            message += body
        message += "\x00"
        return message

    def parse_stomp_message(self, raw_message):
        if not raw_message:
            return None, {}, None

        message = raw_message.rstrip("\x00")
        lines = message.split("\n")

        if not lines:
            return None, {}, None

        command = lines[0]
        headers = {}
        body_start = 1

        for i, line in enumerate(lines[1:], 1):
            if line == "":
                body_start = i + 1
                break
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key] = value

        body = "\n".join(lines[body_start:]) if body_start < len(lines) else None
        return command, headers, body

    async def connect(self):
        try:
            uri = f"wss://api.godelterminal.com/events?jwt={self.jwt_token}"
            headers = {
                "Upgrade": "websocket",
                "Origin": "https://app.godelterminal.com",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Protocol": "v12.stomp, v11.stomp, v10.stomp",
            }

            self.session = aiohttp.ClientSession()
            self.ws = await self.session.ws_connect(uri, headers=headers)
            log_message("WebSocket connected successfully", "INFO")
            self.reconnect_attempts = 0
            return True
        except Exception as e:
            log_message(f"Failed to connect to WebSocket: {e}", "ERROR")
            await send_alert(f"WebSocket connection failed: {e}")
            return False

    async def send_connect(self):
        if not self.ws or self.ws.closed:
            return False

        headers = {
            "Authorization": f"Bearer {self.jwt_token}",
            "accept-version": "1.2,1.1,1.0",
            "heart-beat": "10000,10000",
        }

        connect_message = self.format_stomp_message("CONNECT", headers)
        await self.ws.send_str(connect_message)

        try:
            msg = await self.ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                command, headers, body = self.parse_stomp_message(msg.data)
                if command == "CONNECTED":
                    log_message("STOMP connection established", "INFO")
                    return True
                else:
                    log_message(f"Unexpected STOMP response: {command}", "ERROR")
                    return False
        except Exception as e:
            log_message(f"Error receiving CONNECTED response: {e}", "ERROR")
            await send_alert(f"STOMP handshake failed: {e}")
            return False

        return False

    async def subscribe_to_chat(self):
        if not self.ws or self.ws.closed:
            return False

        headers = {
            "id": "sub-0",
            "destination": f"/topic/chat_events/{CHAT_CHANNEL_ID}",
        }

        subscribe_message = self.format_stomp_message("SUBSCRIBE", headers)
        await self.ws.send_str(subscribe_message)
        log_message(f"Subscribed to chat events for channel: {CHAT_CHANNEL_ID}", "INFO")
        return True

    def extract_message_info(self, body):
        try:
            data = json.loads(body)
            if data.get("type") == "ChatMessageCreated":
                chat_msg = data.get("chatMessage", {})
                return {
                    "content": chat_msg.get("content", ""),
                    "username": chat_msg.get("username", ""),
                    "userId": chat_msg.get("userId", ""),
                    "createdAt": chat_msg.get("createdAt", ""),
                    "messageId": chat_msg.get("id", ""),
                }
        except json.JSONDecodeError:
            log_message("Failed to parse JSON message", "ERROR")
        return None

    async def process_martin_message(self, msg_info):
        timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

        message = f"<b>Martin's Signal from Godel</b>\n\n"
        message += f"<b>Current Time:</b> {timestamp}\n"
        message += f"<b>User:</b> {msg_info['username']}\n"
        message += f"<b>Content:</b> {msg_info['content']}\n"
        message += f"<b>Message ID:</b> {msg_info['messageId']}\n"

        await send_ticker_deck_message(
            sender="godel",
            name="Godel #Paid",
            content=msg_info["content"],
        )

        await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
        log_message(
            f"Martin's message processed and sent: {msg_info['content'][:50]}...",
            "INFO",
        )

    async def listen_for_messages(self):
        log_message("Starting message listener", "INFO")

        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    command, headers, body = self.parse_stomp_message(msg.data)

                    if command == "MESSAGE":
                        destination = headers.get("destination", "")

                        if "/topic/chat_events/" in destination:
                            msg_info = self.extract_message_info(body)
                            if (
                                msg_info
                                and msg_info["username"].lower() == TARGET_USERNAME
                            ):
                                log_message(
                                    f"Martin message detected: {msg_info['content']}",
                                    "INFO",
                                )
                                await self.process_martin_message(msg_info)

                    elif command == "ERROR":
                        log_message(f"STOMP Error: {body}", "ERROR")
                        await send_alert(f"STOMP Error: {body}")

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    error_msg = f"WebSocket error: {self.ws.exception()}"
                    log_message(error_msg, "ERROR")
                    await send_alert(error_msg)
                    break

                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    log_message("WebSocket connection closed", "WARNING")
                    await send_alert("WebSocket connection closed unexpectedly")
                    break

        except asyncio.CancelledError:
            log_message("Message listening cancelled", "INFO")
        except Exception as e:
            error_msg = f"Error listening for messages: {e}"
            log_message(error_msg, "ERROR")
            await send_alert(error_msg)

    async def close(self):
        try:
            if self.ws and not self.ws.closed:
                await self.ws.close()
            if self.session and not self.session.closed:
                await self.session.close()
        except Exception as e:
            log_message(f"Error during cleanup: {e}", "ERROR")

    async def reconnect(self):
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            log_message("Max reconnection attempts reached. Stopping.", "CRITICAL")
            await send_alert(
                "Max reconnection attempts reached. Manual intervention required."
            )
            return False

        self.reconnect_attempts += 1
        log_message(
            f"Attempting to reconnect ({self.reconnect_attempts}/{self.max_reconnect_attempts})",
            "INFO",
        )

        await self.close()
        await asyncio.sleep(10)  # Wait 10 seconds before reconnecting

        return await self.initialize_connection()

    async def initialize_connection(self):
        if not await self.connect():
            return False
        if not await self.send_connect():
            return False
        if not await self.subscribe_to_chat():
            return False
        return True

    async def run_monitoring_session(self):
        while True:
            current_time = get_current_time()
            _, _, market_close_time = get_next_market_times()

            if current_time > market_close_time:
                log_message("Market is closed. Stopping monitoring session.", "INFO")
                break

            if not await self.initialize_connection():
                if not await self.reconnect():
                    break
                continue

            try:
                await self.listen_for_messages()
            except Exception as e:
                log_message(f"Error in monitoring session: {e}", "ERROR")
                await send_alert(f"Monitoring session error: {e}")

                if not await self.reconnect():
                    break

            await asyncio.sleep(5)  # Brief pause before reconnecting

        await self.close()


async def run_monitor():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required Telegram environment variables", "CRITICAL")
        sys.exit(1)

    monitor = GodelChatMonitor()

    while True:
        await sleep_until_market_open()
        await initialize_ticker_deck("Godel")

        log_message("Market is open. Starting Godel chat monitoring...", "INFO")
        log_message(f"Monitoring user: {TARGET_USERNAME}", "INFO")
        log_message(f"Channel ID: {CHAT_CHANNEL_ID}", "INFO")

        try:
            await monitor.run_monitoring_session()
        except Exception as e:
            log_message(f"Critical error in monitoring: {e}", "CRITICAL")
            await send_alert(f"Critical monitoring error: {e}")

        log_message("Market session ended. Waiting for next market open...", "INFO")
        await asyncio.sleep(60)  # Wait a minute before checking market times again


def main():
    try:
        asyncio.run(run_monitor())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
