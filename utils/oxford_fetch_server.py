import asyncio
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Set
from uuid import uuid4

import websockets
from websockets.server import WebSocketServerProtocol

from utils.logger import log_message

CACHE_DIR = Path("data/url_cache")


class WebSocketFetchServer:
    def __init__(self, session, host="0.0.0.0", port=8788):
        self.session = session
        self.host = host
        self.port = port
        self.clients: Set[WebSocketServerProtocol] = set()
        self.server_task = None
        self.setup_cache_dir()

    def setup_cache_dir(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.today_cache_dir = CACHE_DIR / today
        self.today_cache_dir.mkdir(parents=True, exist_ok=True)

        for old_dir in CACHE_DIR.iterdir():
            if old_dir.is_dir() and old_dir.name != today:
                for file in old_dir.glob("*"):
                    file.unlink()
                old_dir.rmdir()
                log_message(f"Cleaned old cache directory: {old_dir.name}", "INFO")

    def get_url_hash(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()

    def get_cached_response(self, url: str) -> Dict:
        url_hash = self.get_url_hash(url)
        cache_file = self.today_cache_dir / f"{url_hash}.json"

        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                    log_message(f"Cache hit for URL: {url}", "INFO")
                    return cached_data
            except Exception as e:
                log_message(f"Error reading cache: {e}", "WARNING")

        return None

    def save_to_cache(self, url: str, response_data: Dict):
        try:
            url_hash = self.get_url_hash(url)
            cache_file = self.today_cache_dir / f"{url_hash}.json"

            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(response_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log_message(f"Error saving to cache: {e}", "WARNING")

    def get_headers(self) -> Dict[str, str]:
        timestamp = int(time.time() * 10000)
        cache_uuid = uuid4()

        return {
            "Connection": "keep-alive",
            "cache-control": "no-cache, no-store, max-age=0, must-revalidate, private",
            "pragma": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36",
            "cache-timestamp": str(timestamp),
            "cache-uuid": str(cache_uuid),
        }

    def fetch_url(self, url: str) -> Dict:
        cached_response = self.get_cached_response(url)
        if cached_response:
            return cached_response

        try:
            response = self.session.get(url, headers=self.get_headers(), timeout=30)

            response_data = {
                "status_code": response.status_code,
                "html": response.text if response.status_code == 200 else "",
                "error": (
                    ""
                    if response.status_code == 200
                    else f"HTTP {response.status_code}"
                ),
            }

            self.save_to_cache(url, response_data)
            log_message(
                f"Fetched and cached URL: {url} - Status: {response.status_code}",
                "INFO",
            )
            return response_data

        except Exception as e:
            error_response = {"status_code": 500, "html": "", "error": str(e)}
            log_message(f"Error fetching URL {url}: {e}", "ERROR")
            return error_response

    async def register_client(self, websocket: WebSocketServerProtocol):
        self.clients.add(websocket)
        log_message(
            f"Fetch client connected. Total clients: {len(self.clients)}", "INFO"
        )

    async def unregister_client(self, websocket: WebSocketServerProtocol):
        self.clients.discard(websocket)
        log_message(
            f"Fetch client disconnected. Total clients: {len(self.clients)}", "INFO"
        )

    def fetch_in_thread(self, url: str) -> Dict:
        return self.fetch_url(url)

    async def handle_client(self, websocket):
        await self.register_client(websocket)

        try:
            async for message in websocket:
                try:
                    if message == "[1":
                        await websocket.send("[2")
                        continue

                    data = json.loads(message)

                    if data.get("type") == "fetch_url":
                        url = data.get("url")
                        if not url:
                            await websocket.send(
                                json.dumps(
                                    {
                                        "status_code": 400,
                                        "html": "",
                                        "error": "No URL provided",
                                    }
                                )
                            )
                            continue

                        loop = asyncio.get_event_loop()
                        response_data = await loop.run_in_executor(
                            None, self.fetch_in_thread, url
                        )

                        await websocket.send(json.dumps(response_data))

                except json.JSONDecodeError:
                    await websocket.send(
                        json.dumps(
                            {"status_code": 400, "html": "", "error": "Invalid JSON"}
                        )
                    )
                except Exception as e:
                    log_message(f"Error handling fetch message: {e}", "ERROR")
                    await websocket.send(
                        json.dumps({"status_code": 500, "html": "", "error": str(e)})
                    )

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self.unregister_client(websocket)

    async def start_server(self):
        log_message(
            f"Starting fetch WebSocket server on {self.host}:{self.port}", "INFO"
        )

        server = await websockets.serve(
            self.handle_client, self.host, self.port, ping_interval=30, ping_timeout=10
        )

        log_message("Fetch WebSocket server started successfully", "INFO")
        return server
