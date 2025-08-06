import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()

# Constants
CHECK_INTERVAL = 1
TELEGRAM_BOT_TOKEN = os.getenv("YOUTUBE_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("YOUTUBE_TELEGRAM_GRP")
PROCESSED_VIDEOS_FILE = "data/processed_youtube_videos.json"
API_KEYS_FILE = "cred/youtube_api_keys.json"
API_USAGE_FILE = "data/youtube_api_usage.json"

os.makedirs("cred", exist_ok=True)
os.makedirs("data", exist_ok=True)

CHANNELS_TO_MONITOR = [
    {
        "name": "Moon Market",
        "uploads_playlist_id": "UUzUTeUSbbTBtj6cgoVaoSeg",
    },
    {
        "name": "6K Investor",
        "uploads_playlist_id": "UUxDsOgAmHnDQw2xS1f1dfcQ",
    },
    {
        "name": "Paul's Portfolio",
        "uploads_playlist_id": "UUJS4uOCQqcjeYIAFdtx7h9Q",
    },
]


class YouTubeMonitor:
    def __init__(self, channels):
        self.channels = channels
        self.api_keys = self.load_api_keys()
        self.current_api_key_index = 0
        self.api_usage = self.load_api_usage()
        self.processed_videos = self.load_processed_videos()

    def load_api_keys(self):
        try:
            with open(API_KEYS_FILE, "r") as f:
                return json.load(f)["api_keys"]
        except FileNotFoundError:
            log_message(f"API keys file not found: {API_KEYS_FILE}", "CRITICAL")
            sys.exit(1)

    def load_api_usage(self):
        try:
            with open(API_USAGE_FILE, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def save_api_usage(self):
        with open(API_USAGE_FILE, "w") as f:
            json.dump(self.api_usage, f, indent=2)

    def load_processed_videos(self):
        try:
            with open(PROCESSED_VIDEOS_FILE, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return []

    def save_processed_videos(self):
        with open(PROCESSED_VIDEOS_FILE, "w") as f:
            json.dump(self.processed_videos, f, indent=2)

    def get_next_available_api_key(self):
        current_time = get_current_time()

        # Clean up expired restrictions
        for key in list(self.api_usage.keys()):
            if datetime.fromisoformat(self.api_usage[key]["reset_time"]) < current_time:
                del self.api_usage[key]
                self.save_api_usage()

        # Try each API key until we find an available one
        for _ in range(len(self.api_keys)):
            current_key = self.api_keys[self.current_api_key_index]
            if current_key not in self.api_usage:
                return current_key

            self.current_api_key_index = (self.current_api_key_index + 1) % len(
                self.api_keys
            )

        return None

    def mark_api_key_exceeded(self, api_key):
        tomorrow = get_current_time().replace(
            hour=8, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)

        self.api_usage[api_key] = {
            "exceeded_time": get_current_time().isoformat(),
            "reset_time": tomorrow.isoformat(),
        }
        self.save_api_usage()

    async def get_recent_videos(self, playlist_id, max_videos=5):
        """Get recent videos from a specific playlist"""
        api_key = self.get_next_available_api_key()
        if not api_key:
            log_message("All API keys are currently restricted", "ERROR")
            return []

        url = (
            f"https://www.googleapis.com/youtube/v3/playlistItems?"
            f"part=snippet&playlistId={playlist_id}&maxResults={max_videos}"
            f"&order=date&key={api_key}"
        )

        try:
            response = requests.get(url)
            if response.status_code == 403:  # API quota exceeded
                self.mark_api_key_exceeded(api_key)
                return await self.get_recent_videos(
                    playlist_id, max_videos
                )  # Retry with next key

            response.raise_for_status()
            videos = response.json().get("items", [])

            # Filter videos published within the last 24 hours
            recent_videos = []
            for video in videos:
                publish_time = video["snippet"]["publishedAt"]
                publish_datetime = datetime.fromisoformat(
                    publish_time.replace("Z", "+00:00")
                )
                time_difference = get_current_time().astimezone() - publish_datetime

                if time_difference.total_seconds() <= 86400:  # Within 24 hours
                    recent_videos.append(video)

            return recent_videos

        except requests.RequestException as e:
            log_message(
                f"Error fetching videos from playlist {playlist_id}: {e}", "ERROR"
            )
            return []

    async def process_new_videos(self):
        """Process new videos from all monitored channels"""
        for channel in self.channels:
            start = time.time()
            videos = await self.get_recent_videos(channel["uploads_playlist_id"])
            log_message(
                f"Fetching recent videos for {channel['name']} took {(time.time() - start):.2f} seconds"
            )

            for video in videos:
                video_id = video["snippet"]["resourceId"]["videoId"]
                video_title = video["snippet"]["title"]

                if video_id not in self.processed_videos:
                    message = (
                        f"<b>New {channel['name']} Video Alert</b>\n\n"
                        f"<b>Channel:</b> {channel['name']}\n"
                        f"<b>Title:</b> {video_title}\n"
                        f"<b>Link:</b> https://youtube.com/watch?v={video_id}"
                    )

                    await send_telegram_message(
                        message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                    )
                    log_message(
                        f"New video alert sent for {channel['name']}: {video_title}",
                        "INFO",
                    )

                    self.processed_videos.append(video_id)
                    self.save_processed_videos()

            await asyncio.sleep(CHECK_INTERVAL)


async def run_youtube_monitor():
    monitor = YouTubeMonitor(CHANNELS_TO_MONITOR)

    for channel in CHANNELS_TO_MONITOR:
        log_message(
            f"Monitoring {channel['name']} - Playlist: {channel['uploads_playlist_id']}",
            "INFO",
        )

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new videos...", "DEBUG")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()

            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                break

            await monitor.process_new_videos()


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_youtube_monitor())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
