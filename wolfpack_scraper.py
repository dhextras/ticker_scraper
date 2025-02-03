import asyncio
import base64
import gzip
import io
import json
import os
import re
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv
from pdfminer.high_level import extract_text

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
BASE_URL = "https://www.wolfpackresearch.com"
API_URL = f"{BASE_URL}/_api"
CHECK_INTERVAL = 1  # seconds
PROCESSED_URLS_FILE = "data/wolfpack_processed_urls.json"
ACCESS_TOKEN_FILE = "data/wolfpack_access_token.json"
TELEGRAM_BOT_TOKEN = os.getenv("WPR_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("WPR_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
TARGET_INT_ID = 2798  # Change this if needed

os.makedirs("data", exist_ok=True)


def extract_ticker_from_titles(title, subtitle):
    # Try subtitle first - multiple patterns
    subtitle_patterns = [
        r"\((?:NYSE|NASDAQ|Nasdaq|Nyse):\s*([A-Z]+)\)",  # (NYSE: ABC) or (NASDAQ: ABC)
        r"\(([A-Z]+)\)",  # (ABC)
        r"(?:NYSE|NASDAQ|Nasdaq|Nyse):\s*([A-Z]+)",  # NASDAQ: ABC without parentheses
    ]

    for pattern in subtitle_patterns:
        match = re.search(pattern, subtitle, re.IGNORECASE)
        if match:
            return match.group(1)

    # If no ticker in subtitle, try title
    title_patterns = [
        r"^([A-Z]+):",  # RILY: blah blah
        r"\(([A-Z]+)\)",  # (ABC)
        r"(?:NYSE|NASDAQ|Nasdaq|Nyse):\s*([A-Z]+)",  # NASDAQ: ABC
    ]

    for pattern in title_patterns:
        match = re.search(pattern, title)
        if match:
            return match.group(1)

    return None


def extract_ticker_from_pdf(text):
    patterns = [
        r"Ticker:\s*(?:NYSE|NASDAQ):\s*([A-Z]+)",  # Ticker: NYSE: ABC
        r"Ticker:\s*([A-Z]+)\s",  # Ticker: ABC US or Ticker: ABC [anything]
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    return None


def load_processed_urls():
    try:
        with open(PROCESSED_URLS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_urls(urls):
    with open(PROCESSED_URLS_FILE, "w") as f:
        json.dump(list(urls), f, indent=2)
    log_message("Processed URLs saved.", "INFO")


def load_access_token():
    try:
        with open(ACCESS_TOKEN_FILE, "r") as f:
            data = json.load(f)
            return data.get("svSession"), data.get("authorization")
    except FileNotFoundError:
        return None, None


def save_access_token(token, auth):
    with open(ACCESS_TOKEN_FILE, "w") as f:
        json.dump({"svSession": token, "authorization": auth}, f)


async def get_access_token(session):
    try:
        async with session.get(f"{API_URL}/v1/access-tokens") as response:
            if response.status == 200:
                data = await response.json()
                sv_session = data["svSession"]

                # Find the target app and extract authorization
                auth_token = None
                for app in data.get("apps", {}).values():
                    if app.get("intId") == TARGET_INT_ID:
                        auth_token = app.get("instance")
                        break

                if auth_token:
                    save_access_token(sv_session, auth_token)
                    return sv_session, auth_token
                else:
                    log_message("Failed to find target app in response", "ERROR")
                    return None, None
    except Exception as e:
        log_message(f"Error getting access token: {e}", "ERROR")
    return None, None


async def get_research_pdfs(session, cookies, auth_token):
    headers = {
        "authorization": auth_token,
        "content-type": "application/json",
    }

    query_data = {
        "urlParams": {
            "gridAppId": "7cbc7368-fc7b-490b-83d7-0bdd71473ecd",
            "viewMode": "site",
        },
        "body": {
            "routerPrefix": "/items",
            "config": {
                "patterns": {
                    "/{title}": {
                        "pageRole": "8beef017-8715-4ba1-bcaf-376f0a54f760",
                        "title": "{title}",
                        "config": {
                            "collection": "Items",
                            "pageSize": 1,
                            "lowercase": True,
                            "sort": [{"title": "asc"}],
                            "seoV2": True,
                        },
                    },
                    "/": {
                        "pageRole": "780c194a-aad3-48a5-8297-aeddb3d214e5",
                        "title": "Items",
                        "config": {
                            "collection": "Items",
                            "pageSize": 10,
                            "sort": [{"date": "desc"}],
                            "lowercase": True,
                            "seoV2": True,
                        },
                    },
                }
            },
            "pageRoles": {
                "8beef017-8715-4ba1-bcaf-376f0a54f760": {
                    "id": "b981k",
                    "title": "Report",
                },
                "780c194a-aad3-48a5-8297-aeddb3d214e5": {
                    "id": "s9tht",
                    "title": "Research",
                },
            },
            "requestInfo": {"env": "browser", "formFactor": "desktop"},
            "routerSuffix": "/",
            "fullUrl": f"{BASE_URL}/items/",
        },
    }

    json_str = json.dumps(query_data)
    compressed = gzip.compress(json_str.encode("utf-8"))
    encoded_data = base64.b64encode(compressed).decode("utf-8")

    try:
        async with session.get(
            f"{API_URL}/dynamic-pages-router/v1/pages?{encoded_data}",
            cookies=cookies,
            headers=headers,
        ) as response:
            if response.status == 200:
                return await response.json()
            if response.status == 304:
                log_message(f"Got cached version, try to bust it", "ERROR")
                return "304"
    except Exception as e:
        log_message(f"Error fetching research PDFs: {e}", "ERROR")
    return None


async def process_pdf_url(pdf_url):
    if not pdf_url.startswith("wix:document://"):
        return pdf_url

    # Extract the relevant parts from the wix document URL
    parts = pdf_url.split("/")
    if len(parts) < 6:
        return None

    ugd_id_part = parts[4]  # e.g., b084d8_8c37d3efbfc54243a7769628f8a2f179.pdf
    return f"{BASE_URL}/_files/ugd/{ugd_id_part}"


async def send_to_telegram_and_ws(article_data):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )

    message = f"<b>New Wolfpack Article Found - API</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>Article Date:</b> {article_data['date']}\n"
    message += f"<b>Title:</b> {article_data['title']}\n"
    message += f"<b>Sub Title:</b> {article_data['subtitle']}\n"
    message += f"<b>Article URL:</b> {article_data['url']}\n"

    if article_data.get("pdf_url"):
        message += f"<b>Pdf URL:</b> {article_data['pdf_url']}\n"

    if article_data.get("ticker"):
        message += f"<b>Ticker:</b> {article_data['ticker']}\n"

    await send_ws_message(
        {
            "name": "Wolfpack Article - API",
            "type": "Buy",
            "ticker": article_data["ticker"],
            "sender": "wolfpack",
            "target": "CSS",
        },
        WS_SERVER_URL,
    )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def run_scraper():
    processed_urls = load_processed_urls()

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new posts...")
        _, _, market_close_time = get_next_market_times()

        async with aiohttp.ClientSession() as session:
            while True:
                current_time = datetime.now(pytz.timezone("America/New_York"))
                if current_time > market_close_time:
                    log_message("Market is closed. Waiting for next market open...")
                    break

                log_message("Checking for new articles...")

                sv_session, auth_token = load_access_token()
                if not sv_session or not auth_token:
                    log_message(
                        "Access Token not available trying to regenerate", "WARNING"
                    )
                    sv_session, auth_token = await get_access_token(session)
                    if not sv_session or not auth_token:
                        log_message("Failed to get access token", "ERROR")
                        await asyncio.sleep(CHECK_INTERVAL)
                        continue

                result = await get_research_pdfs(
                    session, {"svSession": sv_session}, auth_token
                )

                if result == "304":
                    continue

                if not result or result.get("result", {}).get("status") != 200:
                    log_message(
                        "Access Token not available trying to regenerate or fetching failed",
                        "WARNING",
                    )
                    sv_session, auth_token = await get_access_token(session)
                    continue

                items = result.get("result", {}).get("data", {}).get("items", [])
                log_message(f"Fetched {len(items)} items", "INFO")

                for item in items:
                    url = f"{BASE_URL}{item['link-items-title']}"
                    if url in processed_urls:
                        continue

                    article_data = {
                        "date": item.get("date", ""),
                        "title": item.get("title", ""),
                        "subtitle": item.get("subtitle", ""),
                        "url": url,
                        "ticker": extract_ticker_from_titles(
                            item.get("title", ""), item.get("subtitle", "")
                        ),
                    }

                    if item.get("report"):
                        pdf_url = await process_pdf_url(item["report"])
                        if pdf_url:
                            article_data["pdf_url"] = pdf_url
                            if not article_data["ticker"]:
                                try:
                                    async with session.get(pdf_url) as response:
                                        if response.status == 200:
                                            pdf_content = await response.read()
                                            pdf_file = io.BytesIO(pdf_content)
                                            first_page = extract_text(
                                                pdf_file, page_numbers=[0]
                                            )
                                            article_data["ticker"] = (
                                                extract_ticker_from_pdf(first_page)
                                            )
                                except Exception as e:
                                    log_message(f"Error processing PDF: {e}", "ERROR")

                    await send_to_telegram_and_ws(article_data)
                    processed_urls.add(url)
                    save_processed_urls(processed_urls)

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP, WS_SERVER_URL]):
        log_message("Missing required environment variables", "CRITICAL")
        return

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")


if __name__ == "__main__":
    main()
