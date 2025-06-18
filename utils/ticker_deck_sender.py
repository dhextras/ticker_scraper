import asyncio
import json
import os
import re
from pathlib import Path
from typing import List
from urllib.parse import unquote

import aiofiles
import aiohttp
import socketio

from utils.logger import log_message


class TickerDeckWebSocketManager:
    _instance = None
    _sio = None
    _url = None
    _connected = False
    _token = None
    _lock = asyncio.Lock()
    _auth_url = None
    _username = None
    _password = None
    _token_file = "data/ticker_deck_auth_token.txt"

    @classmethod
    async def initialize(cls, custom_name: str) -> bool:
        """Initialize the ticker deck WebSocket connection with authentication"""
        if cls._instance is None:
            cls._instance = cls()

        # Load environment variables
        cls._url = os.getenv("TICKER_DECK_WS_URL")
        cls._auth_url = os.getenv("TICKER_DECK_AUTH_URL")
        cls._username = os.getenv("TICKER_DECK_USERNAME")
        cls._password = os.getenv("TICKER_DECK_PASSWORD")

        if not all([cls._url, cls._auth_url, cls._username, cls._password]):
            log_message(
                "Missing required ticker deck environment variables", "CRITICAL"
            )
            return False

        Path("data").mkdir(exist_ok=True)
        await cls._load_token()

        if cls._token:
            if await cls._test_connection():
                log_message("Existing token is valid", "INFO")
                return True
            else:
                log_message("Existing token is invalid, getting new token", "WARNING")

        if await cls._get_new_token(custom_name):
            return await cls._test_connection()

        return False

    @classmethod
    async def _load_token(cls) -> None:
        """Load token from file if it exists"""
        try:
            if os.path.exists(cls._token_file):
                async with aiofiles.open(cls._token_file, "r") as f:
                    cls._token = (await f.read()).strip()
                    log_message("Token loaded from file", "INFO")
        except Exception as e:
            log_message(f"Error loading token from file: {e}", "ERROR")
            cls._token = None

    @classmethod
    async def _save_token(cls, token: str) -> None:
        """Save token to file"""
        try:
            async with aiofiles.open(cls._token_file, "w") as f:
                await f.write(token)
            log_message("Token saved to file", "INFO")
        except Exception as e:
            log_message(f"Error saving token to file: {e}", "ERROR")

    @classmethod
    async def _get_new_token(cls, custom_name: str) -> bool:
        """Get new authentication token from server"""
        try:
            data = {
                "custom_name": custom_name,
                "username": cls._username,
                "password": cls._password,
            }

            headers = {
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{cls._auth_url}/_root.data?index",
                    data=data,
                    headers=headers,
                    ssl=False,
                ) as response:
                    if response.status == 202:
                        # Extract token from set-cookie header
                        set_cookie = response.headers.get("set-cookie", "")
                        if "ticker_deck_session=" in set_cookie:
                            cookie_value = set_cookie.split("ticker_deck_session=")[
                                1
                            ].split(";")[0]
                            decoded_cookie = unquote(cookie_value)
                            cls._token = decoded_cookie
                            await cls._save_token(cls._token)
                            log_message("New token acquired successfully", "INFO")
                            return True
                        else:
                            log_message("No token found in response cookie", "ERROR")
                            return False
                    else:
                        log_message(
                            f"Authentication failed with status: {response.status}",
                            "ERROR",
                        )
                        return False

        except Exception as e:
            log_message(f"Error getting new token: {e}", "ERROR")
            return False

    @classmethod
    async def _test_connection(cls) -> bool:
        """Test Socket.IO connection with current token"""
        try:
            if cls._token.startswith("{"):
                token_data = json.loads(cls._token)
                jwt_token = token_data.get("token", "")
            else:
                jwt_token = cls._token

            sio = socketio.AsyncClient()

            try:
                await sio.connect(cls._url, auth={"token": jwt_token})
                log_message("Connection test successful", "INFO")
                await sio.disconnect()
                return True

            except Exception as e:
                log_message(f"Socket.IO connection failed: {e}", "ERROR")
                return False

        except Exception as e:
            log_message(f"Connection test failed: {e}", "ERROR")
            return False

    @classmethod
    async def _connect(cls) -> bool:
        """Establish connection to Socket.IO server"""
        if cls._connected:
            return True

        async with cls._lock:
            if cls._connected:
                return True

            try:
                if cls._token.startswith("{"):
                    token_data = json.loads(cls._token)
                    jwt_token = token_data.get("token", "")
                else:
                    jwt_token = cls._token

                cls._sio = socketio.AsyncClient()

                await cls._sio.connect(cls._url, auth={"token": jwt_token})
                cls._connected = True
                log_message(f"Connected to ticker deck Socket.IO at {cls._url}", "INFO")
                return True

            except Exception as e:
                log_message(f"Connection failed: {e}", "ERROR")
                cls._connected = False
                cls._sio = None
                return False

    @classmethod
    async def _disconnect(cls) -> None:
        """Disconnect from Socket.IO server"""
        if cls._sio:
            try:
                await cls._sio.disconnect()
                log_message("Disconnected from ticker deck Socket.IO", "INFO")
            except Exception as e:
                log_message(f"Error during disconnect: {e}", "ERROR")

        cls._connected = False
        cls._sio = None

    @classmethod
    def _extract_tickers(cls, text: str) -> List[str]:
        """Extract ticker symbols from text"""
        if not text:
            return []

        tickers = set()

        # Pattern for exchange-prefixed tickers: (NASDAQ: AAPL), NYSE: GOOGL, etc.
        exchange_patterns = [
            r"\((?:NASDAQ|NYSE|Nasdaq|Nyse):\s*([A-Z]{2,6})\)",  # (NASDAQ: AAPL)
            r"\((?:NASDAQ|NYSE|Nasdaq|Nyse)-([A-Z]{2,6})\)",  # (NYSE-AAPL)
            r"(?:NASDAQ|NYSE|Nasdaq|Nyse):\s*([A-Z]{2,6})",  # NASDAQ: AAPL
            r"(?:NASDAQ|NYSE|Nasdaq|Nyse)-([A-Z]{2,6})",  # NYSE-AAPL
        ]

        for pattern in exchange_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                if len(match.strip()) <= 6:
                    tickers.add(match.strip().upper())

        # Pattern for standalone tickers (words in all caps, 2-6 letters)
        standalone_pattern = r"\b[A-Z]{2,6}\b"
        potential_tickers = re.findall(standalone_pattern, text)

        common_words = {
            "ON",
            "PULL",
            "LEARN",
            "EXPECT",
            "HELP",
            "SEEN",
            "HEAR",
            "NEED",
            "LEAD",
            "TODAY",
            "USE",
            "AMONG",
            "CONCERNING",
            "CONSIDER",
            "MAY",
            "DIE",
            "GOT",
            "BESIDE",
            "LEAVE",
            "BRING",
            "MOVE",
            "DONT",
            "AS",
            "OPPOSITE",
            "SERVE",
            "INSIDE",
            "HAD",
            "FOLLOWING",
            "BEYOND",
            "HE",
            "TOWARDS",
            "GROW",
            "NOR",
            "EXCLUDING",
            "AT",
            "OF",
            "BE",
            "IT",
            "SEND",
            "IS",
            "LIKE",
            "WALK",
            "SHOW",
            "REPORT",
            "GONE",
            "LOOK",
            "FROM",
            "AGAINST",
            "FEEL",
            "WITH",
            "CREATE",
            "ACROSS",
            "AI",
            "THE",
            "BEFORE",
            "WOULD",
            "UNTO",
            "CONTINUE",
            "UNTIL",
            "MAKE",
            "BECOME",
            "LOSE",
            "WORK",
            "MIGHT",
            "CAME",
            "UNDERNEATH",
            "AMID",
            "PASS",
            "REMEMBER",
            "BEHIND",
            "FOLLOW",
            "DECIDE",
            "CAN",
            "NOW",
            "MEAN",
            "BEING",
            "SHOULD",
            "WILL",
            "WAS",
            "ASOF",
            "BELOW",
            "AND",
            "WONT",
            "UNDERSTAND",
            "NO",
            "ALLOW",
            "PUT",
            "OFFER",
            "FALL",
            "CEO",
            "OFF",
            "SEE",
            "GO",
            "PAY",
            "STAY",
            "UNLIKE",
            "TALK",
            "STAND",
            "ROUND",
            "SIT",
            "DOES",
            "SEEM",
            "BUT",
            "DESPITE",
            "APPEAR",
            "REMAIN",
            "DO",
            "SHALL",
            "STOP",
            "SELL",
            "WENT",
            "ALL",
            "INCLUDE",
            "ABOVE",
            "DID",
            "CUT",
            "RUN",
            "CONSIDERING",
            "SPEND",
            "WRITE",
            "YOU",
            "CHANGE",
            "GET",
            "DURING",
            "ARE",
            "KEEP",
            "NOT",
            "BETWEEN",
            "INTO",
            "HAVE",
            "REQUIRE",
            "CANT",
            "LOVE",
            "SPEAK",
            "REACH",
            "TOWARD",
            "KNOW",
            "COULD",
            "SET",
            "UP",
            "DOWN",
            "WAIT",
            "LET",
            "SUGGEST",
            "OUTSIDE",
            "SINCE",
            "FOR",
            "OUTOF",
            "ABOUT",
            "MADE",
            "HAS",
            "SO",
            "KILL",
            "TILL",
            "BEEN",
            "ADD",
            "EXCEPT",
            "LIVE",
            "THINK",
            "ANY",
            "OR",
            "SHE",
            "PLAY",
            "ONTO",
            "UPON",
            "SAW",
            "ALONG",
            "GIVE",
            "TOOK",
            "MUST",
            "TO",
            "RAISE",
            "REGARDING",
            "IN",
            "BY",
            "WE",
            "YET",
            "ASK",
            "OVER",
            "PAST",
            "VERSUS",
            "BESIDES",
            "BUILD",
            "UPDATE",
            "BEGIN",
            "START",
            "READ",
            "UNDER",
            "BENEATH",
            "COME",
            "AROUND",
            "VIA",
            "OPEN",
            "THROUGHOUT",
            "THAN",
            "CALL",
            "NEAR",
            "WIN",
            "TRY",
            "WANT",
            "WITHOUT",
            "WERE",
            "TAKE",
            "HAPPEN",
            "MEET",
            "TURN",
            "THROUGH",
            "BUY",
            "AM",
            "AFTER",
            "TAKEN",
            "THEY",
            "OUT",
            "WITHIN",
            "WATCH",
        }

        for ticker in potential_tickers:
            if ticker not in common_words and len(ticker) <= 6:
                tickers.add(ticker.upper())

        # Look for quoted tickers: "GOOGL", 'AAPL'
        quoted_patterns = [r'"([A-Z]{2,6})"', r"'([A-Z]{2,6})'"]

        for pattern in quoted_patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                if len(match.strip()) <= 6:
                    tickers.add(match.strip().upper())

        # Pattern for dollar-prefixed tickers like $AAPL
        dollar_pattern = r"\$([A-Z]{2,6})"
        dollar_matches = re.findall(dollar_pattern, text)
        for match in dollar_matches:
            if len(match.strip()) <= 6:
                tickers.add(match.strip().upper())

        # Look for tickers with separators: -SOFT-, HAVE-SOFT
        separator_pattern = r"(?:^|[^A-Z])[A-Z]{2,6}(?:-[A-Z]{2,6})*(?=[^A-Z]|$)"
        separator_matches = re.findall(separator_pattern, text)
        for match in separator_matches:
            parts = match.split("-")
            for part in parts:
                if part and len(part) <= 6 and part not in common_words:
                    tickers.add(part.upper())

        # Do a senatization one more time
        finalized_ticker = []
        for ticker in tickers:
            ticker = ticker.strip().upper()
            if ticker not in common_words and len(ticker) <= 6 and len(ticker) >= 2:
                finalized_ticker.append(ticker)

        return sorted(list(set(finalized_ticker)))

    @classmethod
    async def send_ticker_deck_message(
        cls, sender: str, name: str = "", title: str = "", content: str = ""
    ) -> bool:
        """Send a message to the ticker deck"""
        if not sender or not sender.strip():
            log_message("Sender cannot be empty", "ERROR")
            return False

        sender = sender.strip()

        if not name or not name.strip():
            name = sender.capitalize()
        else:
            name = name.strip()

        title = title.strip() if title else ""
        content = content.strip() if content else ""

        if not title and not content:
            log_message("Both title and content cannot be empty", "ERROR")
            return False

        tickers = []
        if title:
            tickers.extend(cls._extract_tickers(title))
        if content:
            tickers.extend(cls._extract_tickers(content))

        message = {
            "sender": sender,
            "name": name,
            "tickers": tickers,
            "title": title,
            "content": content,
        }

        try:
            if not await cls._connect():
                log_message("Failed to connect to ticker deck Socket.IO", "ERROR")
                return False

            await cls._sio.emit("send_trading_message", message)
            log_message(f"Ticker deck message sent successfully from {sender}", "INFO")

            # FIXME: Remove this later if we don't need it but basically waiting for the message to be sent
            await asyncio.sleep(2)
            await cls._disconnect()
            return True

        except Exception as e:
            log_message(f"Failed to send ticker deck message: {e}", "ERROR")
            await cls._disconnect()
            return False


async def initialize_ticker_deck(custom_name: str) -> bool:
    """Initialize ticker deck WebSocket connection"""
    return await TickerDeckWebSocketManager.initialize(custom_name)


async def send_ticker_deck_message(
    sender: str, name: str = "", title: str = "", content: str = ""
) -> bool:
    """Send a message to the ticker deck"""
    return await TickerDeckWebSocketManager.send_ticker_deck_message(
        sender, name, title, content
    )
