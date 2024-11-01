import asyncio
import json
import os
import sys
import time
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("CNBC_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CNBC_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
GMAIL_USERNAME = os.getenv("CNBC_SCRAPER_GMAIL_USERNAME")
ARTICLE_DATA_SHA = os.getenv("CNBC_SCRAPER_ARTICLE_DATA_SHA")
SESSION_TOKEN = os.getenv("CNBC_SCRAPER_SESSION_TOKEN")

# Article ID file path
ARTICLE_ID_FILE = "cred/cnbc_latest_article_id.json"


def load_article_id():
    try:
        if os.path.exists(ARTICLE_ID_FILE):
            with open(ARTICLE_ID_FILE, "r") as f:
                data = json.load(f)
                return data.get("article_id", None)
        else:
            # Ensure directory exists
            print("hello")
            os.makedirs(os.path.dirname(ARTICLE_ID_FILE), exist_ok=True)
            return None
    except Exception as e:
        log_message(f"Error loading article ID: {e}", "ERROR")
        return None


def save_article_id(article_id):
    try:
        os.makedirs(os.path.dirname(ARTICLE_ID_FILE), exist_ok=True)
        with open(ARTICLE_ID_FILE, "w") as f:
            json.dump({"article_id": article_id}, f)
    except Exception as e:
        log_message(f"Error saving article ID: {e}", "ERROR")


class RateLimiter:
    def __init__(self, calls_per_second=2):
        self.calls_per_second = calls_per_second
        self.last_call_time = 0
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            current_time = time.time()
            time_since_last_call = current_time - self.last_call_time
            if time_since_last_call < (1 / self.calls_per_second):
                await asyncio.sleep((1 / self.calls_per_second) - time_since_last_call)
            self.last_call_time = time.time()


rate_limiter = RateLimiter()


async def get_article_data(article_id, uid, session_token):
    await rate_limiter.acquire()
    base_url = "https://webql-redesign.cnbcfm.com/graphql"
    variables = {
        "id": article_id,
        "uid": uid,
        "sessionToken": session_token,
        "pid": 33,
        "bedrockV3API": True,
        "sponsoredProExperienceID": "",
    }
    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": ARTICLE_DATA_SHA,
        }
    }
    params = {
        "operationName": "getArticleData",
        "variables": json.dumps(variables),
        "extensions": json.dumps(extensions),
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(base_url, params=params) as response:
                if response.status == 200:
                    response_json = await response.json()
                    article_data = response_json.get("data", {}).get("article")

                    if article_data is None:
                        log_message(f"Article {article_id} is null", "INFO")
                        return None, None

                    article_type = article_data.get("type")
                    if article_type != "cnbcnewsstory":
                        log_message(
                            f"Article {article_id} is type: {article_type}", "INFO"
                        )
                        return None, "wrong_type"

                    # Process article body for cnbcnewsstory type
                    article_body = article_data.get("body", {}).get("content", [])

                    if article_body:
                        for content_block in article_body:
                            if content_block.get("tagName") == "div":
                                for child in content_block.get("children", []):
                                    if child.get("tagName") == "blockquote":
                                        paragraph = child.get("children", [])[0]
                                        if paragraph.get("tagName") == "p":
                                            text = "".join(
                                                [
                                                    (
                                                        part
                                                        if isinstance(part, str)
                                                        else part.get("children", [])[0]
                                                    )
                                                    for part in paragraph.get(
                                                        "children", []
                                                    )
                                                ]
                                            )
                                            return text, "success"
                    return None, "no_content"
                else:
                    log_message(
                        f"Error fetching article {article_id}: {response.status}",
                        "ERROR",
                    )
                    return None, "error"
        except Exception as e:
            log_message(
                f"Exception in get_article_data for article {article_id}: {e}", "ERROR"
            )
            return None, "error"


async def process_article(article_content, article_id):
    try:
        if article_content:
            published_date = datetime.now(pytz.utc)
            current_time = datetime.now(pytz.utc)

            message = (
                f"<b>New Article Alert!</b>\n"
                f"<b>Article ID:</b> {article_id}\n"
                f"<b>Published Date:</b> {published_date.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S.%f %Z')}\n"
                f"<b>Content:</b> {article_content}\n"
            )

            await asyncio.gather(
                send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID),
                send_ws_message(
                    {
                        "name": "CNBC",
                        "type": "Buy",
                        "ticker": str(article_id),
                        "sender": "cnbc",
                    },
                    WS_SERVER_URL,
                ),
            )
            return True
    except Exception as e:
        log_message(f"Error processing article {article_id}: {e}", "ERROR")
    return False


async def run_article_monitor(uid, session_token):
    current_article_id = load_article_id()
    if not current_article_id:
        log_message("Starting article id point couldn't be found", "CRITICAL")
        sys.exit(1)

    log_message(f"Starting with article ID: {current_article_id}", "INFO")

    while True:
        try:
            # Wait until market open
            await sleep_until_market_open()
            log_message("Market is open. Starting to check articles...")

            # Get market close time
            _, _, market_close_time = get_next_market_times()

            # Main market hours loop
            while True:
                current_time = datetime.now(pytz.timezone("America/New_York"))
                if current_time > market_close_time:
                    log_message("Market is closed. Waiting for next market open...")
                    break

                content, status = await get_article_data(
                    current_article_id, uid, session_token
                )

                if status == "error":
                    await asyncio.sleep(5)  # Longer sleep on error
                    continue
                elif status == "wrong_type":
                    current_article_id += 1
                    save_article_id(current_article_id)
                    await asyncio.sleep(1)
                elif status == "no_content":
                    current_article_id += 1
                    save_article_id(current_article_id)
                    await asyncio.sleep(1)
                elif status is None:  # Article is null
                    await asyncio.sleep(1)  # Don't increment ID for null articles
                else:  # Success
                    await process_article(content, current_article_id)
                    current_article_id += 1
                    save_article_id(current_article_id)
                    await asyncio.sleep(1)

        except Exception as e:
            log_message(f"Error in monitor loop: {e}", "ERROR")
            await asyncio.sleep(5)


def main():
    uid = GMAIL_USERNAME

    if not all(
        [
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            WS_SERVER_URL,
            SESSION_TOKEN,
            GMAIL_USERNAME,
            ARTICLE_DATA_SHA,
        ]
    ):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_article_monitor(uid, SESSION_TOKEN))
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
