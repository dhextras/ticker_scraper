import asyncio
import random
import threading
import time
import uuid

import requests
from bs4 import BeautifulSoup

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_current_time

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.59",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 OPR/78.0.4093.112",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/91.0.4472.80 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
]


def get_random_headers():
    """Generate random headers for requests"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": str(uuid.uuid4()),
        "X-Request-Time": str(int(time.time())),
    }


def get_random_cache_buster():
    """Generate random cache busting url variable for requests"""
    cache_busters = [
        ("timestamp", lambda: int(time.time() * 10000)),
        ("request_uuid", lambda: str(uuid.uuid4())),
        ("cache_time", lambda: int(time.time())),
        ("ran_time", lambda: int(time.time() * 1000)),
        ("no_cache_uuid", lambda: str(uuid.uuid4().hex[:16])),
        ("unique", lambda: f"{int(time.time())}-{random.randint(1000, 9999)}"),
        ("req_uuid", lambda: f"req-{uuid.uuid4().hex[:8]}"),
        ("tist", lambda: str(int(time.time()))),
    ]

    variable, value_generator = random.choice(cache_busters)
    return f"{variable}={value_generator()}"


def convert_draft_url_to_public(draft_url):
    """Convert draft URL to public URL format"""
    if "/publish/post/" in draft_url:
        post_id = draft_url.split("/publish/post/")[1]
        return f"https://thebearcave.substack.com/p/{post_id}"
    return draft_url


def fetch_draft_post_info(url, headers):
    """Fetch draft post information with error handling"""
    try:
        random_cache_buster = get_random_cache_buster()

        response = requests.get(f"{url}?{random_cache_buster}", headers=headers)
        current_time = get_current_time().strftime("%Y-%m-%d %H:%M:%S %Z")

        try:
            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title.text if soup.title else None
        except:
            title = None

        return {
            "url": response.url,
            "status_code": response.status_code,
            "timestamp": current_time,
            "title": title,
        }
    except Exception as e:
        current_time = get_current_time().strftime("%Y-%m-%d %H:%M:%S %Z")
        return {
            "url": url,
            "status_code": "ERROR",
            "timestamp": current_time,
            "title": None,
            "error": str(e),
        }


def monitor_draft_post(
    draft_url, telegram_token, telegram_group, source_identifier, headers
):
    """Monitor draft post for 3 seconds and send results via Telegram"""

    def run_monitoring():
        public_url = convert_draft_url_to_public(draft_url)
        log_message(f"Starting draft monitoring for: {public_url}", "INFO")

        results = []
        last_result = None

        start_time = time.time()

        while time.time() - start_time < 3:
            result = fetch_draft_post_info(public_url, headers)

            # Only add result if it's different from the last one
            if last_result is None or (
                result["status_code"] != last_result["status_code"]
                or result["title"] != last_result["title"]
                or result["url"] != last_result["url"]
            ):
                results.append(result)
                last_result = result

            time.sleep(0.1)

        if results:
            message = f"<b>Draft Monitoring - {source_identifier}</b>\n\n"
            message += f"<b>Original Draft URL:</b> {draft_url}\n"
            message += f"<b>Public URL:</b> {public_url}\n"
            message += f"<b>Total Unique Results:</b> {len(results)}\n\n"

            for i, result in enumerate(results, 1):
                message += f"<b>Result {i}:</b>\n"
                message += f"<b>Time:</b> {result['timestamp']}\n"
                message += f"<b>Status:</b> {result['status_code']}\n"
                message += f"<b>URL:</b> {result['url']}\n"
                message += f"<b>Title:</b> {result['title'] or 'None'}\n"
                if "error" in result:
                    message += f"<i>Error:</i> {result['error']}\n"
                message += "\n"

            asyncio.run(send_telegram_message(message, telegram_token, telegram_group))
            log_message(
                f"Draft monitoring results sent to Telegram for: {public_url}", "INFO"
            )

    thread = threading.Thread(target=run_monitoring, daemon=True)
    thread.start()
    log_message(
        f"Draft monitoring started in background thread for: {draft_url}", "INFO"
    )


async def start_monitoring(
    draft_url, telegram_token, telegram_group, source_identifier
):
    headers = get_random_headers()

    monitor_draft_post(
        draft_url, telegram_token, telegram_group, source_identifier, headers
    )
