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
    _ping_interval = 5  # 1 minute ping-pong check
    _lock = asyncio.Lock()
    _ping_task = None

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

        # Start the keep-alive and ping-pong tasks
        if cls._ping_task is None or cls._ping_task.done():
            cls._ping_task = asyncio.create_task(cls._ping_pong_check())

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
    async def _ping_pong_check(cls) -> None:
        """Perform manual ping-pong check every minute"""
        while True:
            try:
                await asyncio.sleep(cls._ping_interval)

                if not cls._connected or cls._connection is None:
                    log_message(
                        f"Not connected during ping check, reconnecting",
                        "WARNING",
                    )
                    await cls._reset_connection()
                    continue

                # Send ping message (1)
                await cls._connection.send(json.dumps("[1"))

                try:
                    response = await asyncio.wait_for(cls._connection.recv(), timeout=5)

                    # Check if we got the expected pong value (2)
                    if response != "[2" and "ticker" not in response:
                        log_message(f"Unexpected pong response: {response}", "WARNING")
                        await cls._reset_connection()
                except asyncio.TimeoutError:
                    log_message("Ping-pong timeout - no response received", "WARNING")
                    await cls._reset_connection()
                except Exception as e:
                    log_message(f"Error during ping-pong resetting: {e}", "WARNING")
                    await cls._reset_connection()

            except Exception as e:
                log_message(f"Ping-pong check failed: {e}", "WARNING")
                await asyncio.sleep(cls._reconnect_interval)
                await cls._reset_connection()

    @classmethod
    async def _reset_connection(cls) -> None:
        """Reset and reconnect the WebSocket connection"""
        if cls._connection:
            try:
                await cls._connection.close()
                log_message("Closed connection due to failed ping-pong", "INFO")
            except Exception as e:
                log_message(f"Error closing connection: {e}", "ERROR")

        cls._connected = False
        cls._connection = None
        await cls._connect()

    @classmethod
    async def _keep_alive(cls) -> None:
        while True:
            try:
                if cls._connected and cls._connection:
                    await asyncio.sleep(cls._keep_alive_interval)
                else:
                    # TODO: Change this to a warning as well and increase the sleep time if not warning detected
                    log_message(f"Connection is not alive reconnecting...", "CRITICAL")
                    await cls._reset_connection()
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
        raise ValueError("Missing WS_SERVER_URL environment variable")
    await WebSocketManager.initialize(WS_SERVER_URL)


async def send_ws_message(message: Dict[Any, Any]) -> None:
    """Send a message over the WebSocket connection"""
    await WebSocketManager.send_message(message)
