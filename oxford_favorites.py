import asyncio
import json
import os
import random
import sys
import uuid
from datetime import datetime
from time import time
from typing import Dict, List, Optional

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
STARTING_ID = 134784  # Starting post ID
FAVORITES_WINDOW = 50  # Number of favorites to maintain
BATCH_SIZE = 10
BATCH_DELAY = 0.1  # Delay between batch operations (seconds)
CHECK_INTERVAL = 0.3  # Interval to check favorites page (seconds)

# File paths
DATA_DIR = "data"
FAVORITES_STATE_FILE = f"{DATA_DIR}/oxford_favorites_state.json"
HTML_BACKUP_DIR = f"{DATA_DIR}/remove"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(HTML_BACKUP_DIR, exist_ok=True)


class FavoritesState:
    def __init__(self):
        self.current_start_id = STARTING_ID
        self.current_end_id = STARTING_ID + FAVORITES_WINDOW - 1
        self.known_post_ids = set()
        self.current_favorites = set()
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
) -> None:
    """Check favorites page and update favorites list accordingly"""
    log_message(
        f"Checking favorites page - Current range: {state.current_start_id}-{state.current_end_id}",
        "INFO",
    )

    html_content = await fetch_favorites_page(session)
    if not html_content:
        return

    current_favorites = parse_favorites_html(html_content)
    if not current_favorites:
        log_message("No favorites found on page", "WARNING")
        return

    current_post_ids = {fav["post_id"] for fav in current_favorites}

    new_post_ids = current_post_ids - state.known_post_ids
    if new_post_ids:
        log_message(f"Found New posts ids: {str(new_post_ids)}", "INFO")
        new_articles = [
            fav for fav in current_favorites if fav["post_id"] in new_post_ids
        ]
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
        # FIXME: Remove this HTML backup feature later once script is confirmed working
        backup_path = save_html_backup(html_content)
        log_message(
            f"Found {len(new_post_ids)} new articles {(len(current_post_ids))}, {(len(state.known_post_ids))}, HTML backed up to: {backup_path}",
            "INFO",
        )

    # Update known post IDs
    state.known_post_ids.update(current_post_ids)
    state.save_state()


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
    await check_and_update_favorites(session, state)


async def run_favorites_manager() -> None:
    """Main function to run the favorites manager"""
    state = FavoritesState()

    session = requests.Session()
    if not login_sync(session):
        return

    if state.known_post_ids:
        available_start = int(max(state.known_post_ids))
        state.update_range(available_start)
        state.save_state()

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

            await check_and_update_favorites(session, state)
            await asyncio.sleep(CHECK_INTERVAL)


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
