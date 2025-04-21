import asyncio
import json
import os
from typing import Any, Dict

import websockets

from utils.logger import log_message


class WebSocketManager:
    _instance = None
    _connection = None
    _url = None
    _connected = False
    _reconnect_interval = 1
    _keep_alive_interval = 1800  # 30 min
    _lock = asyncio.Lock()

    @classmethod
    async def initialize(cls, url: str) -> None:
        """Initialize the WebSocket connection"""
        if cls._instance is None:
            cls._instance = cls()

        # If a connection already exists, close and reset it
        if cls._connected and cls._connection:
            try:
                await cls._connection.close()
                log_message(
                    "Existing WebSocket connection closed for reinitialization.", "INFO"
                )
            except Exception as e:
                log_message(
                    f"Error closing existing WebSocket connection: {e}", "ERROR"
                )

        cls._connection = None
        cls._connected = False

        cls._url = url
        await cls._connect()

        asyncio.create_task(cls._keep_alive())

    @classmethod
    async def _connect(cls) -> None:
        """Establish connection to WebSocket server"""
        if cls._connected:
            return

        async with cls._lock:
            if cls._connected:  # Double-check inside lock
                return

            try:
                cls._connection = await websockets.connect(
                    cls._url,
                    ping_interval=30,
                    ping_timeout=10,
                )
                cls._connected = True
                log_message(f"Connected to WebSocket at {cls._url}", "INFO")
            except Exception as e:
                log_message(f"Connection failed: {e}", "ERROR")
                cls._connected = False
                cls._connection = None

    @classmethod
    async def _keep_alive(cls) -> None:
        while True:
            try:
                if cls._connected and cls._connection:
                    await asyncio.sleep(cls._keep_alive_interval)
                else:
                    # TODO: Change this to a warning as well and increase the sleep time if not warning detected
                    log_message(f"Connection is not alive reconnecting...", "CRITICAL")
                    cls._connected = False
                    cls._connection = None

                if not cls._connected:
                    await cls._connect()
                    await asyncio.sleep(cls._reconnect_interval)
            except Exception as e:
                log_message(f"Error in keep_alive: {e}", "ERROR")
                await asyncio.sleep(cls._reconnect_interval)

    @classmethod
    async def send_message(cls, message: Dict[Any, Any]) -> None:
        """Send a message over the WebSocket connection"""
        if not cls._url:
            raise ValueError("WebSocket not initialized. Call initialize() first.")

        if not cls._connected:
            # TODO: Just make it a warning later on
            log_message(
                "Connection is not alive.. trying to reconnect check the server",
                "CRITICAL",
            )
            await cls._connect()

        try:
            await cls._connection.send(json.dumps(message))
        except Exception as e:
            log_message(f"Failed to send message: {e}", "ERROR")
            cls._connected = False
            cls._connection = None
            raise


async def initialize_websocket() -> None:
    WS_SERVER_URL = os.getenv("WS_SERVER_URL")
    if not WS_SERVER_URL:
        log_message("Missing required WS server url", "CRITICAL")
        raise

    await WebSocketManager.initialize(WS_SERVER_URL)


async def send_ws_message(message: Dict[Any, Any]) -> None:
    """Send a message over the WebSocket connection"""
    await WebSocketManager.send_message(message)
