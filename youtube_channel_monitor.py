import asyncio
import json
import os
import sys
import time
from datetime import datetime

import pytz
import requests
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open

load_dotenv()

# Constants
CHECK_INTERVAL = 1
TELEGRAM_BOT_TOKEN = os.getenv("YOUTUBE_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("YOUTUBE_TELEGRAM_GRP")
PLAYLIST_ID = os.getenv("YOUTUBE_PLAYLIST_ID")
PROCESSED_VIDEOS_FILE = "data/processed_youtube_videos.json"
API_KEYS_FILE = "cred/youtube_api_keys.json"
API_USAGE_FILE = "data/youtube_api_usage.json"

os.makedirs("cred", exist_ok=True)
os.makedirs("data", exist_ok=True)


class YouTubeMonitor:
    def __init__(self, playlist_id):
        self.playlist_id = playlist_id
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
        current_time = datetime.now(pytz.UTC)

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
        tomorrow = datetime.now(pytz.UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        tomorrow = tomorrow.replace(day=tomorrow.day + 1)

        self.api_usage[api_key] = {
            "exceeded_time": datetime.now(pytz.UTC).isoformat(),
            "reset_time": tomorrow.isoformat(),
        }
        self.save_api_usage()

    async def get_recent_videos(self, max_videos=5):
        api_key = self.get_next_available_api_key()
        if not api_key:
            log_message("All API keys are currently restricted", "ERROR")
            return []

        url = (
            f"https://www.googleapis.com/youtube/v3/playlistItems?"
            f"part=snippet&playlistId={self.playlist_id}&maxResults={max_videos}"
            f"&order=date&key={api_key}"
        )

        try:
            response = requests.get(url)
            if response.status_code == 403:  # API quota exceeded
                self.mark_api_key_exceeded(api_key)
                return await self.get_recent_videos(max_videos)  # Retry with next key

            response.raise_for_status()
            videos = response.json().get("items", [])

            # Filter videos published within the last 24 hours
            recent_videos = []
            for video in videos:
                publish_time = video["snippet"]["publishedAt"]
                publish_datetime = datetime.fromisoformat(
                    publish_time.replace("Z", "+00:00")
                )
                time_difference = datetime.now().astimezone() - publish_datetime

                if time_difference.total_seconds() <= 86400:  # Within 24 hours
                    recent_videos.append(video)

            return recent_videos

        except requests.RequestException as e:
            log_message(f"Error fetching videos: {e}", "ERROR")
            return []

    async def process_new_videos(self):
        start = time.time()
        videos = await self.get_recent_videos()
        log_message(f"Fetching recent video took {(time.time() - start):.2f} seconds")

        for video in videos:
            video_id = video["snippet"]["resourceId"]["videoId"]
            video_title = video["snippet"]["title"]

            if video_id not in self.processed_videos:
                message = (
                    f"<b>New Moon Market Video Alert</b>\n\n"
                    f"<b>Title:</b> {video_title}\n"
                    f"<b>Link:</b> https://youtube.com/watch?v={video_id}"
                )

                await send_telegram_message(
                    message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                )
                log_message(f"New video alert sent: {video_title}", "INFO")

                self.processed_videos.append(video_id)
                self.save_processed_videos()


async def run_youtube_monitor():
    monitor = YouTubeMonitor(PLAYLIST_ID)

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new videos...")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))

            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...")
                break

            await monitor.process_new_videos()
            await asyncio.sleep(CHECK_INTERVAL)


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
