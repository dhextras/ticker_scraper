import asyncio
import io
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp
from dotenv import load_dotenv
from PyPDF2 import PdfReader

from utils.bypass_cloudflare import bypasser
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message

load_dotenv()

# Constants
BASE_URL = "https://www.muddywatersresearch.com/wp-content/uploads"
TELEGRAM_BOT_TOKEN = os.getenv("MUDDY_WATERS_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("MUDDY_WATERS_TELEGRAM_GRP")
SESSION_FILE = "data/muddy_waters_session.json"
PROCESSED_PDFS_FILE = "data/muddy_waters_processed_pdfs.json"
CHECK_INTERVAL = 3  # seconds

os.makedirs("data", exist_ok=True)


def load_cookies(frash=False) -> Optional[Dict[str, Any]]:
    try:
        cookies = None
        if frash == False:
            if not os.path.exists(SESSION_FILE):
                log_message(f"Session file not found: {SESSION_FILE}", "WARNING")
            else:
                with open(SESSION_FILE, "r") as f:
                    cookies = json.load(f)

        # Validate cookies again
        if not cookies or cookies.get("cf_clearance", "") == "":
            log_message(
                "Invalid or missing 'cf_clearance' in cookies. Attempting to regenerate.",
                "WARNING",
            )
            bypass = bypasser(BASE_URL, SESSION_FILE)

            if not bypass or bypass == False:
                return

            with open(SESSION_FILE, "r") as f:
                cookies = json.load(f)

            if not cookies or cookies.get("cf_clearance", "") == "":
                return None

        return cookies

    except Exception as e:
        log_message(f"Error loading session: {e}", "ERROR")
    return None


def load_processed_pdfs():
    try:
        with open(PROCESSED_PDFS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_pdfs(pdfs):
    with open(PROCESSED_PDFS_FILE, "w") as f:
        json.dump(list(pdfs), f, indent=2)
    log_message("Processed PDFs saved.", "INFO")


async def send_telegram_notification(url, title):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = (
        f"<b>Muddy Waters PDF Found</b>\n\n"
        f"<b>Time:</b> {timestamp}\n"
        f"<b>URL:</b> {url}\n"
        f"<b>Title:</b> {title}"
    )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"PDF notification sent: {url}", "INFO")


async def download_pdf(session, url, cookies):
    try:
        headers = {
            "User-Agent": f"{cookies['user_agent']}",
            "Cookie": f"cf_clearance:{cookies['cf_clearance']}",
        }

        async with session.get(url, headers=headers, cookies=cookies) as response:
            if response.status == 200:
                pdf_content = await response.read()

                # Read PDF title
                pdf_reader = PdfReader(io.BytesIO(pdf_content))
                first_page = pdf_reader.pages[0]
                title = first_page.extract_text().split("\n")[0].strip()

                await send_telegram_notification(url, title)

                return title

            elif response.status == 403:
                cookies = load_cookies(frash=True)
                if not cookies:
                    raise Exception(f"CF_CLEARANCE Failed for URL: {url}")
                return cookies

            elif response.status == 404:
                log_message(f"PDF not found: {url}", "WARNING")
                return None

            else:
                log_message(f"Failed to fetch PDF: HTTP {response.status}", "ERROR")
                return None

    except Exception as e:
        log_message(f"Error processing PDF {url}: {e}", "ERROR")
        return None


async def fetch_pdfs_for_dates(session, cookies, date=None):
    if not date:
        date = datetime.now()

    processed_pdfs = load_processed_pdfs()

    # Try different date formats
    date_formats = [
        date.strftime("%d%m%Y"),  # ddmmyyyy
        date.strftime("%Y%m%d"),  # yyyymmdd
    ]

    for date_str in date_formats:
        url = (f"{BASE_URL}/{date.year}/{date.month:02d}/MW_{date_str}.pdf",)

        if url in processed_pdfs:
            continue

        log_message(f"Attempting to fetch: {url}", "INFO")
        title = await download_pdf(session, url, cookies)

        if title is not None and isinstance(title, dict):
            cookies = title
        else:
            processed_pdfs.add(url)

    save_processed_pdfs(processed_pdfs)
    return cookies


async def run_pdf_fetcher():
    cookies = load_cookies()

    if not cookies:
        log_message("Failed to get valid cf_clearance", "CRITICAL")
        return

    async with aiohttp.ClientSession() as session:
        while True:
            # NOTE: send the custom date if needed
            # custom_date = datetime(datetime.now().year, 12, 28)
            # cookies = await fetch_pdfs_for_dates(session, cookies, custom_date)

            cookies = await fetch_pdfs_for_dates(session, cookies)
            await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_pdf_fetcher())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
