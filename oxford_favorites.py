import asyncio
import json
import os
import random
import re
import sys
import uuid
from datetime import datetime
from time import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import requests
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

# Constants
FAVORITES_URL = "https://oxfordclub.com/favorites"
FAVORITES_API_URL = "https://oxfordclub.com/favorites/?handle_favorites"
LOGIN_URL = "https://oxfordclub.com/wp-login.php"
USERNAME = os.getenv("OXFORDCLUB_USERNAME")
PASSWORD = os.getenv("OXFORDCLUB_PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("OXFORDCLUB_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("OXFORDCLUB_TELEGRAM_GRP")

# Configuration
STARTING_ID = 135720  # Starting post ID
FAVORITES_WINDOW = 50  # Number of favorites to maintain
BATCH_SIZE = 10
BATCH_DELAY = 0.1  # Delay between batch operations (seconds)
CHECK_INTERVAL = 0.3  # Interval to check favorites page (seconds)
MAX_NO_FAVORITES_COUNT = 10  # Restart threshold

# File paths
DATA_DIR = "data"
FAVORITES_STATE_FILE = f"{DATA_DIR}/oxford_favorites_state.json"

os.makedirs(DATA_DIR, exist_ok=True)


class FavoritesState:
    def __init__(self):
        self.current_start_id = STARTING_ID
        self.current_end_id = STARTING_ID + FAVORITES_WINDOW - 1
        self.known_post_ids = set()
        self.current_favorites = set()
        self.no_favorites_count = 0
        self.load_state()

    def load_state(self):
        try:
            with open(FAVORITES_STATE_FILE, "r") as f:
                data = json.load(f)
                self.current_start_id = data.get("current_start_id", STARTING_ID)
                self.current_end_id = data.get(
                    "current_end_id", STARTING_ID + FAVORITES_WINDOW - 1
                )
                self.known_post_ids = set(data.get("known_post_ids", []))
                self.current_favorites = set(data.get("current_favorites", []))
                self.no_favorites_count = data.get("no_favorites_count", 0)
                log_message(
                    f"Loaded state: Range {self.current_start_id}-{self.current_end_id}, Known posts: {len(self.known_post_ids)}",
                    "INFO",
                )
        except FileNotFoundError:
            log_message("No previous state found, starting fresh", "INFO")
            self.save_state()

    def save_state(self):
        data = {
            "current_start_id": self.current_start_id,
            "current_end_id": self.current_end_id,
            "known_post_ids": list(self.known_post_ids),
            "current_favorites": list(self.current_favorites),
            "no_favorites_count": self.no_favorites_count,
        }
        with open(FAVORITES_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def update_range(self, new_highest_id: int):
        """Update the favorites range based on new highest ID found"""
        old_start = self.current_start_id
        old_end = self.current_end_id

        if new_highest_id > old_start:
            self.current_start_id = new_highest_id
            self.current_end_id = new_highest_id + FAVORITES_WINDOW - 1

            log_message(
                f"Updated range: {old_start}-{old_end} → {self.current_start_id}-{self.current_end_id}",
                "INFO",
            )
            self.save_state()

    def reset_operational_counters_only(self):
        """Reset only operational counters, preserve known post IDs and favorites data"""
        self.no_favorites_count = 0
        self.save_state()
        log_message(
            "Operational counters reset (preserved known posts and favorites data)",
            "INFO",
        )

    def reset_state(self):
        """Reset state for restart - ONLY use this for complete fresh start"""
        self.current_start_id = STARTING_ID
        self.current_end_id = STARTING_ID + FAVORITES_WINDOW - 1
        self.known_post_ids = set()
        self.current_favorites = set()
        self.no_favorites_count = 0
        self.save_state()
        log_message("State completely reset for fresh start", "INFO")


async def send_alert(msg: str):
    alert = f"ALERT: {msg}"
    await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


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
    timestamp = int(time() * 10000)
    cache_uuid = uuid4()

    return {
        "Connection": "keep-alive",
        "cache-control": "no-cache, no-store, max-age=0, must-revalidate, private",
        "pragma": "no-cache",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36",
        "cache-timestamp": str(timestamp),
        "cache-uuid": str(cache_uuid),
    }


async def process_page(
    session: requests.Session, url: str
) -> Optional[Tuple[str, str, str, float]]:
    try:
        start_time = time()
        response = await asyncio.to_thread(
            session.get, url, headers=get_headers(), timeout=15
        )
        total_seconds = time() - start_time

        if response.status_code == 200:
            content = response.text
            soup = BeautifulSoup(content, "html.parser")
            all_text = soup.get_text(separator=" ", strip=True)

            action_sections = re.split(r"Action to Take", all_text, flags=re.IGNORECASE)

            if len(action_sections) < 2:
                log_message(f"'Action to Take' not found: {url}", "WARNING")

            for section in action_sections[1:]:
                buy_match = re.search(r"Buy", section, re.IGNORECASE)
                sell_match = re.search(r"Sell", section, re.IGNORECASE)
                ticker_match = re.search(
                    r"(NYSE|NASDAQ):\s*(\w+)", section, re.IGNORECASE
                )

                if (
                    sell_match
                    and ticker_match
                    and sell_match.start() < ticker_match.start()
                ):
                    exchange, ticker = ticker_match.groups()
                    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")
                    await send_match_to_telegram(
                        url, ticker, exchange, "Sell", timestamp, total_seconds
                    )
                    return (ticker, exchange, "Sell", total_seconds)
                elif (
                    buy_match
                    and ticker_match
                    and buy_match.start() < ticker_match.start()
                ):
                    exchange, ticker = ticker_match.groups()
                    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")
                    await send_match_to_telegram(
                        url, ticker, exchange, "Buy", timestamp, total_seconds
                    )
                    return (ticker, exchange, "Buy", total_seconds)

            log_message(
                f"Took {total_seconds:.2f}s to fetch and process URL: {url}", "WARNING"
            )
        else:
            log_message(f"Failed to fetch page: HTTP {response.status_code}", "ERROR")
    except Exception as e:
        log_message(f"Error processing page {url}: {e}", "ERROR")

    return None


async def send_match_to_telegram(
    url: str,
    ticker: str,
    exchange: str,
    action: str,
    timestamp: str,
    total_seconds: float,
) -> None:

    await send_ws_message(
        {
            "name": "Oxford Club - Favorite ID",
            "type": action,
            "ticker": ticker,
            "sender": "oxfordclub",
        },
    )

    message = f"<b>New Stock Match Found - Favorite ID</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>URL:</b> {url}\n"
    message += f"<b>Stock Symbol:</b> {exchange}:{ticker}\n"
    message += f"<b>Article Fetch time:</b> {total_seconds:.2f}s\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Match sent to Telegram: {exchange}:{ticker} - {url}", "INFO")


async def add_favorite(session: requests.Session, post_id: int) -> bool:
    """Add a post to favorites"""
    try:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": "https://oxfordclub.com",
            "referer": "https://oxfordclub.com/favorites/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
        }

        data = {"post": str(post_id), "action": "add"}

        response = await asyncio.to_thread(
            session.post, FAVORITES_API_URL, headers=headers, data=data, timeout=10
        )

        if response.status_code == 200:
            return True
        else:
            log_message(
                f"Failed to add favorite {post_id}: HTTP {response.status_code}",
                "ERROR",
            )
            return False
    except Exception as e:
        log_message(f"Error adding favorite {post_id}: {e}", "ERROR")
        return False


async def remove_favorite(session: requests.Session, post_id: int) -> bool:
    """Remove a post from favorites"""
    try:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": "https://oxfordclub.com",
            "referer": "https://oxfordclub.com/favorites/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
        }
        data = {"post": str(post_id), "action": "remove"}

        response = await asyncio.to_thread(
            session.post, FAVORITES_API_URL, headers=headers, data=data, timeout=10
        )

        if response.status_code == 200:
            return True
        else:
            log_message(
                f"Failed to remove favorite {post_id}: HTTP {response.status_code}",
                "ERROR",
            )
            return False
    except Exception as e:
        log_message(f"Error removing favorite {post_id}: {e}", "ERROR")
        return False


async def add_favorites_batch(session: requests.Session, post_ids: List[int]) -> None:
    """Add favorites in batches with delay"""
    log_message(
        f"Adding {len(post_ids)} favorites: {post_ids[0]}-{post_ids[-1]}", "INFO"
    )

    for i in range(0, len(post_ids), BATCH_SIZE):
        batch = post_ids[i : i + BATCH_SIZE]
        tasks = [add_favorite(session, post_id) for post_id in batch]
        results = await asyncio.gather(*tasks)

        success_count = sum(1 for result in results if result)
        log_message(
            f"Batch {i//BATCH_SIZE + 1}: Added {success_count}/{len(batch)} favorites",
            "INFO",
        )

        if i + BATCH_SIZE < len(post_ids):  # Don't sleep after last batch
            await asyncio.sleep(BATCH_DELAY)


async def remove_favorites_batch(
    session: requests.Session, post_ids: List[int]
) -> None:
    """Remove favorites in batches with delay"""
    log_message(
        f"Removing {len(post_ids)} favorites: {post_ids[0]}-{post_ids[-1]}", "INFO"
    )

    for i in range(0, len(post_ids), BATCH_SIZE):
        batch = post_ids[i : i + BATCH_SIZE]
        tasks = [remove_favorite(session, post_id) for post_id in batch]
        results = await asyncio.gather(*tasks)

        success_count = sum(1 for result in results if result)
        log_message(
            f"Batch {i//BATCH_SIZE + 1}: Removed {success_count}/{len(batch)} favorites",
            "INFO",
        )

        if i + BATCH_SIZE < len(post_ids):  # Don't sleep after last batch
            await asyncio.sleep(BATCH_DELAY)


def get_random_cache_buster():
    cache_busters = [
        ("cache_timestamp", lambda: int(time() * 10000)),
        ("request_uuid", lambda: str(uuid.uuid4())),
        ("cache_time", lambda: int(time())),
        ("ran_time", lambda: int(time() * 1000)),
        ("no_cache_uuid", lambda: str(uuid.uuid4().hex[:16])),
        ("unique", lambda: f"{int(time())}-{random.randint(1000, 9999)}"),
        ("req_uuid", lambda: f"req-{uuid.uuid4().hex[:8]}"),
        ("tist", lambda: str(int(time()))),
    ]

    variable, value_generator = random.choice(cache_busters)
    return variable, value_generator()


async def fetch_favorites_page(session: requests.Session) -> Optional[str]:
    """Fetch the favorites page HTML"""
    try:
        key, value = get_random_cache_buster()
        url = f"{FAVORITES_URL}?{key}={value}"
        response = await asyncio.to_thread(session.get, url, timeout=15)

        if response.status_code == 200:
            return response.text
        else:
            log_message(
                f"Failed to fetch favorites page: HTTP {response.status_code}", "ERROR"
            )
            return None
    except Exception as e:
        log_message(f"Error fetching favorites page: {e}", "ERROR")
        return None


def parse_favorites_html(html_content: str) -> List[Dict[str, str]]:
    """Parse favorites page HTML to extract post information"""
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        favorites = []

        links = soup.select("body > div.page-section.members-content > div > div > a")

        for link in links:
            try:
                href = link.get("href", "").strip()

                star_element = link.select_one("span.favorite-star")
                if not star_element:
                    continue

                post_id = star_element.get("data-post", "").strip()
                if not post_id:
                    continue

                title_element = link.select_one("span.favorite-title")
                title = (
                    title_element.get_text(strip=True) if title_element else "No Title"
                )

                favorites.append(
                    {"post_id": int(post_id), "title": title, "link": href}
                )

            except (ValueError, AttributeError) as e:
                log_message(f"Error parsing favorite item: {e}", "WARNING")
                continue

        return favorites
    except Exception as e:
        log_message(f"Error parsing favorites HTML: {e}", "ERROR")
        return []


def save_html_backup(html_content: str) -> str:
    """Save HTML content with timestamp filename"""
    timestamp = datetime.now().strftime("%d_%m_%Y_%H_%M")
    filename = f"{timestamp}.html"
    filepath = os.path.join(HTML_BACKUP_DIR, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_content)
        return filepath
    except Exception as e:
        log_message(f"Error saving HTML backup: {e}", "ERROR")
        return ""


async def send_new_articles_to_telegram(new_articles: List[Dict[str, str]]) -> None:
    """Send new articles found to Telegram"""
    if not new_articles:
        return

    current_time = get_current_time().strftime("%Y-%m-%d %H:%M:%S.%f")

    message = f"<b>({len(new_articles)}) New Oxford favorite found</b>\n\n"

    for article in new_articles:
        message += f"<b>ID:</b> {article['post_id']}\n"
        message += f"<b>Title:</b> {article['title']}\n"
        message += f"<b>Link:</b> {article['link']}\n"
        message += f"<b>current Time:</b> {current_time}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Sent {len(new_articles)} new articles to Telegram", "INFO")


async def check_and_update_favorites(
    session: requests.Session, state: FavoritesState
) -> bool:
    """Check favorites page and update favorites list accordingly. Returns True if restart needed."""
    log_message(
        f"Checking favorites page - Current range: {state.current_start_id}-{state.current_end_id}",
        "INFO",
    )

    html_content = await fetch_favorites_page(session)
    if not html_content:
        return False

    current_favorites = parse_favorites_html(html_content)
    if not current_favorites:
        state.no_favorites_count += 1
        log_message(
            f"No favorites found on page (count: {state.no_favorites_count})", "WARNING"
        )

        if state.no_favorites_count >= MAX_NO_FAVORITES_COUNT:
            log_message(
                f"No favorites error threshold reached ({state.no_favorites_count}). Triggering restart.",
                "ERROR",
            )
            await send_alert(
                f"No favorites error hit {state.no_favorites_count} times - restarting scraper"
            )
            return True

        state.save_state()
        return False

    # Reset counter if we found favorites
    if state.no_favorites_count > 0:
        log_message(
            f"Favorites found again, resetting no_favorites_count from {state.no_favorites_count} to 0",
            "INFO",
        )
        state.no_favorites_count = 0

    current_post_ids = {fav["post_id"] for fav in current_favorites}

    new_post_ids = current_post_ids - state.known_post_ids
    if new_post_ids:
        log_message(f"Found New posts ids: {str(new_post_ids)}", "INFO")
        new_articles = [
            fav for fav in current_favorites if fav["post_id"] in new_post_ids
        ]
        for article in new_articles:
            await process_page(session, article["link"])

        await send_new_articles_to_telegram(new_articles)

        highest_new_id = int(max(new_post_ids))
        if highest_new_id > state.current_start_id:
            # Calculate what we need to add (from current_end_id + 1 to highest_new_id + FAVORITES_WINDOW - 1)
            new_start = highest_new_id
            new_end = highest_new_id + FAVORITES_WINDOW - 1

            # Add new favorites (only the ones we don't already have)
            ids_to_add = []
            for post_id in range(new_start + 1, new_end + 1):
                if post_id not in state.current_favorites:
                    ids_to_add.append(post_id)

            if ids_to_add:
                await add_favorites_batch(session, ids_to_add)
                state.current_favorites.update(ids_to_add)

            # Remove old favorites (below the new start)
            ids_to_remove = []
            for post_id in state.current_favorites:
                if post_id <= new_start:
                    ids_to_remove.append(post_id)

            if ids_to_remove:
                await remove_favorites_batch(session, ids_to_remove)
                state.current_favorites.difference_update(ids_to_add)

            available_start = int(max(new_post_ids))
            state.update_range(available_start)
            state.save_state()

    if new_post_ids:
        log_message(
            f"Found {len(new_post_ids)} new articles {(len(current_post_ids))}, {(len(state.known_post_ids))}",
            "INFO",
        )

    # Update known post IDs
    state.known_post_ids.update(current_post_ids)
    state.save_state()
    return False


async def initialize_favorites(
    session: requests.Session, state: FavoritesState
) -> None:
    """Initialize favorites by adding the initial range"""
    log_message(
        f"Initializing favorites with range: {state.current_start_id}-{state.current_end_id}",
        "INFO",
    )

    ids_to_add = [
        i
        for i in range(state.current_start_id, state.current_end_id + 1)
        if i not in state.current_favorites
    ]

    if ids_to_add:
        await add_favorites_batch(session, ids_to_add)
        state.current_favorites.update(ids_to_add)
        state.save_state()

    await asyncio.sleep(1)  # just to make sure the favorite load in the first go
    needs_restart = await check_and_update_favorites(session, state)
    if needs_restart:
        raise Exception("Restart needed during initialization")


async def run_favorites_manager() -> None:
    """Main function to run the favorites manager"""
    while True:
        try:
            state = FavoritesState()
            session = requests.Session()

            if not login_sync(session):
                log_message("Login failed, retrying in 30 seconds...", "ERROR")
                await asyncio.sleep(30)
                continue

            if state.known_post_ids:
                available_start = int(max(state.known_post_ids))
                state.update_range(available_start)
                state.save_state()

            await initialize_websocket()
            await initialize_favorites(session, state)

            while True:
                await sleep_until_market_open()

                log_message("Market is open. Starting favorites monitoring...", "DEBUG")
                _, _, market_close_time = get_next_market_times()

                while True:
                    current_time = get_current_time()

                    if current_time > market_close_time:
                        log_message(
                            "Market is closed. Waiting for next market open...", "DEBUG"
                        )
                        break

                    needs_restart = await check_and_update_favorites(session, state)
                    if needs_restart:
                        raise Exception("Restart triggered by no favorites threshold")

                    await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            log_message(f"Error in favorites manager, restarting: {e}", "ERROR")
            try:
                state = FavoritesState()
                state.reset_operational_counters_only()
                log_message(
                    f"Preserved data on restart: {len(state.known_post_ids)} known posts, range {state.current_start_id}-{state.current_end_id}",
                    "INFO",
                )

                session.close()
                log_message(
                    "Session closed and operational counters reset, restarting in 10 seconds...",
                    "INFO",
                )
            except Exception as cleanup_error:
                log_message(
                    f"Error during cleanup: {cleanup_error}, continuing with restart...",
                    "WARNING",
                )

            await asyncio.sleep(10)
            continue


def main() -> None:
    if not all([USERNAME, PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_favorites_manager())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
