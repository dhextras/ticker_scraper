import asyncio
import base64
import gzip
import io
import json
import os
import re
import time
import xml.etree.ElementTree as ET
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
CHECK_INTERVAL = 1
PROCESSED_URLS_FILE = "data/wolfpack_xml_processed_urls.json"
ACCESS_TOKEN_FILE = "data/wolfpack_xml_access_token.json"
TELEGRAM_BOT_TOKEN = os.getenv("WPR_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("WPR_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

os.makedirs("data", exist_ok=True)


def load_access_token():
    try:
        with open(ACCESS_TOKEN_FILE, "r") as f:
            data = json.load(f)
            return data.get("svSession")
    except FileNotFoundError:
        return None


def save_access_token(token):
    with open(ACCESS_TOKEN_FILE, "w") as f:
        json.dump({"svSession": token}, f)


async def get_access_token(session):
    try:
        async with session.get(f"{API_URL}/v1/access-tokens") as response:
            if response.status == 200:
                data = await response.json()
                save_access_token(data["svSession"])
                return data["svSession"]
    except Exception as e:
        log_message(f"Error getting access token: {e}", "ERROR")
    return None


def extract_ticker_from_titles(title, subtitle):
    subtitle_patterns = [
        r"\((?:NYSE|NASDAQ|Nasdaq|Nyse):\s*([A-Z]+)\)",
        r"\(([A-Z]+)\)",
        r"(?:NYSE|NASDAQ|Nasdaq|Nyse):\s*([A-Z]+)",
    ]

    for pattern in subtitle_patterns:
        match = re.search(pattern, subtitle, re.IGNORECASE)
        if match:
            return match.group(1)

    title_patterns = [
        r"^([A-Z]+):",
        r"\(([A-Z]+)\)",
        r"(?:NYSE|NASDAQ|Nasdaq|Nyse):\s*([A-Z]+)",
    ]

    for pattern in title_patterns:
        match = re.search(pattern, title)
        if match:
            return match.group(1)

    return None


def extract_ticker_from_pdf(text):
    patterns = [
        r"Ticker:\s*(?:NYSE|NASDAQ):\s*([A-Z]+)",
        r"Ticker:\s*([A-Z]+)\s",
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


async def process_pdf_url(pdf_url):
    if not pdf_url.startswith("wix:document://"):
        return pdf_url

    parts = pdf_url.split("/")
    if len(parts) < 6:
        return None

    ugd_id_part = parts[4]
    return f"{BASE_URL}/_files/ugd/{ugd_id_part}"


async def get_article_details(session, url, access_token):
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
                        "seoMetaTags": {
                            "description": "{subtitle}",
                            "og:image": "{image}",
                            "keywords": "",
                            "robots": "index",
                        },
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
                        "seoMetaTags": {"robots": "index"},
                        "pageRole": "780c194a-aad3-48a5-8297-aeddb3d214e5",
                        "title": "Items",
                        "config": {
                            "collection": "Items",
                            "pageSize": 1,
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
            "routerSuffix": url.replace(f"{BASE_URL}/items", ""),
            "fullUrl": url,
        },
    }

    json_str = json.dumps(query_data)
    compressed = gzip.compress(json_str.encode("utf-8"))
    encoded_data = base64.b64encode(compressed).decode("utf-8")

    try:
        headers = {"Cookie": f"svSession={access_token}"} if access_token else {}
        async with session.get(
            f"{API_URL}/dynamic-pages-router/v1/pages?{encoded_data}", headers=headers
        ) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("result", {}).get("status") == 200:
                    return data.get("result", {}).get("data", {}).get("items", [])[0]
    except Exception as e:
        log_message(f"Error fetching article details: {e}", "ERROR")

    return None


async def send_to_telegram_and_ws(article_data, process_time):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )

    message = f"<b>New Wolfpack Article Found - XML</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>Article Date:</b> {article_data['date']}\n"
    message += f"<b>Title:</b> {article_data['title']}\n"
    message += f"<b>Sub Title:</b> {article_data['subtitle']}\n"
    message += f"<b>Article URL:</b> {article_data['url']}\n"
    message += f"<b>Fetch and Process Time:</b> {process_time:.2f} seconds\n"

    if article_data.get("pdf_url"):
        message += f"<b>Pdf URL:</b> {article_data['pdf_url']}\n"

    if article_data.get("ticker"):
        message += f"<b>Ticker:</b> {article_data['ticker']}\n"

    await send_ws_message(
        {
            "name": "Wolfpack Article - XML",
            "type": "Buy",
            "ticker": article_data["ticker"],
            "sender": "wolfpack",
            "target": "CSS",
        },
        WS_SERVER_URL,
    )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def fetch_sitemap(session):
    async with session.get(f"{BASE_URL}/dynamic-items-sitemap.xml") as response:
        if response.status == 200:
            content = await response.text()
            root = ET.fromstring(content)
            urls = [
                url.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc").text
                for url in root
            ]
            return urls
    return []


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

                access_token = load_access_token()
                if not access_token:
                    access_token = await get_access_token(session)

                fetch_start_time = time.time()
                urls = await fetch_sitemap(session)
                fetch_time = time.time() - fetch_start_time
                log_message(f"Fetched {len(urls)} articles.")

                for url in urls:
                    if url in processed_urls:
                        continue

                    process_start_time = time.time()

                    article = await get_article_details(session, url, access_token)
                    if not article:
                        # Try refreshing token and retry once
                        access_token = await get_access_token(session)
                        article = await get_article_details(session, url, access_token)
                        if not article:
                            continue

                    article_data = {
                        "date": article.get("date", ""),
                        "title": article.get("title", ""),
                        "subtitle": article.get("subtitle", ""),
                        "url": url,
                        "ticker": extract_ticker_from_titles(
                            article.get("title", ""), article.get("subtitle", "")
                        ),
                    }

                    if article.get("report"):
                        pdf_url = await process_pdf_url(article["report"])
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

                    process_time = (time.time() - process_start_time) + fetch_time
                    await send_to_telegram_and_ws(article_data, process_time)
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
