import asyncio
import json
import os
from typing import Dict, Optional

import websockets
from websockets.client import WebSocketClientProtocol

from utils.logger import log_message


class WebSocketFetchClient:
    _instance = None
    _connection: Optional[WebSocketClientProtocol] = None
    _url = None
    _connected = False
    _reconnect_interval = 2
    _ping_interval = 60
    _lock = asyncio.Lock()
    _ping_task = None

    @classmethod
    async def initialize(cls, url: str) -> None:
        if cls._instance is None:
            cls._instance = cls()

        if cls._connected and cls._connection:
            try:
                await cls._connection.close()
                log_message(
                    "Existing fetch WebSocket connection closed for reinitialization",
                    "INFO",
                )
            except Exception as e:
                log_message(f"Error closing existing fetch WebSocket: {e}", "ERROR")

        cls._connection = None
        cls._connected = False
        cls._url = url
        await cls._connect()

        if cls._ping_task is None or cls._ping_task.done():
            cls._ping_task = asyncio.create_task(cls._ping_pong_check())

    @classmethod
    async def _connect(cls) -> None:
        if cls._connected:
            return

        async with cls._lock:
            if cls._connected:
                return

            try:
                cls._connection = await websockets.connect(
                    cls._url,
                    ping_interval=30,
                    ping_timeout=10,
                )
                cls._connected = True
                log_message(f"Connected to fetch WebSocket at {cls._url}", "INFO")
            except Exception as e:
                log_message(f"Fetch WebSocket connection failed: {e}", "ERROR")
                cls._connected = False
                cls._connection = None

    @classmethod
    async def _ping_pong_check(cls) -> None:
        while True:
            try:
                await asyncio.sleep(cls._ping_interval)

                if not cls._connected or cls._connection is None:
                    log_message(
                        "Fetch WebSocket not connected during ping check, reconnecting",
                        "WARNING",
                    )
                    await cls._reset_connection()
                    continue

                await cls._connection.send("[1")

                try:
                    response = await asyncio.wait_for(cls._connection.recv(), timeout=5)
                    if response != "[2":
                        log_message(f"Unexpected pong response: {response}", "WARNING")
                        await cls._reset_connection()
                except asyncio.TimeoutError:
                    log_message("Fetch WebSocket ping-pong timeout", "WARNING")
                    await cls._reset_connection()

            except Exception as e:
                log_message(f"Fetch WebSocket ping-pong check failed: {e}", "WARNING")
                await cls._reset_connection()
                await asyncio.sleep(cls._reconnect_interval)

    @classmethod
    async def _reset_connection(cls) -> None:
        if cls._connection:
            try:
                await cls._connection.close()
                log_message("Closed fetch WebSocket due to failed ping-pong", "INFO")
            except Exception as e:
                log_message(f"Error closing fetch WebSocket: {e}", "ERROR")

        cls._connected = False
        cls._connection = None
        await cls._connect()

    @classmethod
    async def fetch_url(cls, url: str, timeout: int = 15) -> Dict[str, any]:
        if not cls._url:
            raise ValueError(
                "Fetch WebSocket not initialized. Call initialize() first."
            )

        if not cls._connected:
            log_message(
                "Fetch WebSocket not connected, attempting to reconnect", "WARNING"
            )
            await cls._connect()

        if not cls._connected:
            return {
                "status_code": 503,
                "html": "",
                "error": "WebSocket connection failed",
            }

        try:
            request = {"type": "fetch_url", "url": url}

            await cls._connection.send(json.dumps(request))

            response = await asyncio.wait_for(cls._connection.recv(), timeout=timeout)
            return json.loads(response)

        except asyncio.TimeoutError:
            log_message(f"Timeout waiting for response for URL: {url}", "ERROR")
            return {"status_code": 408, "html": "", "error": "Request timeout"}
        except Exception as e:
            log_message(f"Error fetching URL {url}: {e}", "ERROR")
            cls._connected = False
            cls._connection = None
            return {"status_code": 500, "html": "", "error": str(e)}


async def initialize_fetch_websocket() -> None:
    fetch_ws_url = os.getenv("OXFORD_WS_SERVER_URL", "ws://localhost:8788")
    await WebSocketFetchClient.initialize(fetch_ws_url)


async def fetch_url_request(url: str, timeout: int = 15) -> Dict[str, any]:
    return await WebSocketFetchClient.fetch_url(url, timeout)
