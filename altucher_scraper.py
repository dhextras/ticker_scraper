import asyncio
import io
import json
import os
import random
import re
import sys
import time
from datetime import datetime

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pdfminer.high_level import extract_text

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
JSON_URL = "https://my.paradigmpressgroup.com/api/article"
LOGIN_URL = "https://my.paradigmpressgroup.com/api/auth"
USERNAME = os.getenv("ALTUCHER_USERNAME")
PASSWORD = os.getenv("ALTUCHER_PASSWORD")
COOKIE_TID = os.getenv("ALTUCHER_COOKIE_TID")
COOKIE_ID = os.getenv("ALTUCHER_COOKIE_ID")
CHECK_INTERVAL = 0.1  # seconds
PROCESSED_URLS_FILE = "data/paradigm_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("ALTUCHER_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("ALTUCHER_TELEGRAM_GRP")
PROXY_FILE = "cred/proxies.json"

# NOTE: cId stand for the category like
categories = {
    "Alerts": "630ga2Gfm1hh4L2mHMkBHS",
    "Issues": "49RxAGaOXW3MilamPgjp6A",
    "Updates": "4Rnp1u5SB5m9d4WuJkfeiq",
    "Reports": "2KkGfTXI3s3yjQFdU70Xoo",
}

subscriptions = [
    {"name": "mm2", "id": "2rcJUw40n0QEtHPmYrdeeT", "category": "Alerts"},
    {"name": "sei", "id": "32p68JKA43P2tQ0ibAeyDM", "category": "Alerts"},
    {"name": "rbc", "id": "2FshbzKdaVQhH3SAoSwOkn", "category": "Alerts"},
    {"name": "pmg", "id": "4B25WARgTMmaRlOCtJYJso", "category": "Alerts"},
    {"name": "aln", "id": "6GPKqoNr7GKuuoKRpdMf01", "category": "Issues"},
    {"name": "al2", "id": "5CEaime61Vv0QEl5XrRVeb", "category": "Updates"},
    {"name": "taa", "id": "226NJVakKYCxpV8PKbxFdI", "category": "Updates"},
    {"name": "mm2", "id": "2rcJUw40n0QEtHPmYrdeeT", "category": "Reports"},
    {"name": "rbc", "id": "2FshbzKdaVQhH3SAoSwOkn", "category": "Reports"},
    {"name": "sei", "id": "32p68JKA43P2tQ0ibAeyDM", "category": "Reports"},
    {"name": "al2", "id": "5CEaime61Vv0QEl5XrRVeb", "category": "Reports"},
    {"name": "pmg", "id": "4B25WARgTMmaRlOCtJYJso", "category": "Reports"},
]

os.makedirs("data", exist_ok=True)


def load_proxies():
    try:
        with open(PROXY_FILE, "r") as f:
            data = json.load(f)
            proxies = data.get("altucher", [])
            if not proxies:
                log_message("No proxies found in config", "CRITICAL")
                sys.exit(1)
            return proxies
    except FileNotFoundError:
        log_message(f"Proxy file not found: {PROXY_FILE}", "CRITICAL")
        sys.exit(1)
    except json.JSONDecodeError:
        log_message(f"Invalid JSON in proxy file: {PROXY_FILE}", "CRITICAL")
        sys.exit(1)
    except Exception as e:
        log_message(f"Error loading proxies: {e}", "CRITICAL")
        sys.exit(1)


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


async def fetch_articles(session, subscription_name, subscription_id, category, proxy):
    try:
        category_id = categories[category]
        params = {
            "include": 2,
            "order": "-fields.postDate",
            "fields.articleCategory.sys.id": category_id,
            "fields.subscription.sys.id": subscription_id,
            "fields.postDate[gte]": "2020-12-31T18:00:00.000Z",
            "fields.postDate[lte]": "2030-12-31T10:22:51.880Z",
            "skip": 0,
            "limit": 10,
        }

        headers = {
            "Accept": "application/json",
            "Cookie": f"tid={COOKIE_TID}; _dd_s=logs=1&iid={COOKIE_ID}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        }

        proxy_url = f"http://{proxy}" if proxy else None

        start_time = time.time()
        async with session.get(
            JSON_URL, params=params, headers=headers, proxy=proxy_url, timeout=3
        ) as response:
            if response.status == 200:
                try:
                    raw_data = await response.json()
                    processed_data = []
                    log_message(
                        f"Fetched {len(raw_data)} {subscription_name.upper()} {category} using proxy {proxy}, Took {(time.time() - start_time):.2f}s",
                        "INFO",
                    )

                    for stocRecs in raw_data:
                        stocRecs["subscription_name"] = subscription_name
                        processed_data.append(stocRecs)

                    return processed_data
                except:
                    response_text = await response.text()
                    if "loading" in response_text or "spinner" in response_text:
                        log_message(
                            f"Failed to fully load the page for {subscription_name}, with proxy: {proxy}",
                            "WARNING",
                        )
                        return []

                    log_message(
                        f"Failed to extract data for {subscription_name} articles with proxy: {proxy}. raw text:\n\n{response_text}",
                        "ERROR",
                    )
                    return []
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return []
            else:
                log_message(
                    f"Failed to fetch {subscription_name} articles with proxy {proxy}: HTTP {response.status}",
                    "ERROR",
                )
                return []
    except asyncio.TimeoutError:
        log_message(
            f"Took more then 3 sec to fetch {subscription_name} with proxy: {proxy}",
            "WARNING",
        )
        return []
    except Exception as e:
        log_message(
            f"Error fetching {subscription_name} articles with proxy {proxy}: {e}",
            "ERROR",
        )
        return []


async def process_articles(articles, subscription):
    buy_recommendations = []
    for article in articles:
        title = article["title"].lower()
        if (
            title.startswith("buy alert:")
            or title.startswith("flash buy:")
            or title.startswith("new trade alert:")
            # FIXME: This is not a good aproach lol so figure out a way to properly parse later
            or subscription
            in [
                "aln",
                "al2",
                "taa",
            ]
        ):
            if "stockRecommendations" in article:
                for stock_rec in article.get("stockRecommendations", []):
                    if stock_rec["action"].lower() == "buy":
                        buy_recommendations.append(
                            {
                                "ticker": stock_rec["tickerSymbol"],
                                "name": stock_rec["stockName"],
                                "actionDesc": stock_rec["actionDescription"],
                                "postDate": article["cfUpdatedAt"],
                                "url": article["slug"],
                                "subscription_name": article["subscription_name"],
                            }
                        )
            else:
                soup = BeautifulSoup(article["content"], "html.parser")
                action = soup.find("p", class_="buy")
                if action:
                    match = re.search(r"\(([A-Z]+)\)", action.text)
                    if match:
                        buy_recommendations.append(
                            {
                                "ticker": match.group(1),
                                "name": "---Empty---",
                                "actionDesc": "---Empty---",
                                "postDate": article["cfUpdatedAt"],
                                "url": article["slug"],
                                "subscription_name": article["subscription_name"],
                            }
                        )

    return buy_recommendations


async def process_reports(session, articles, subscription_name):
    buy_recommendations = []

    headers = {
        "Accept": "application/json",
        "Cookie": f"tid={COOKIE_TID}; *dd*s=logs=1&iid={COOKIE_ID}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    }

    for article in articles:
        try:
            if not article.get("attachment") or not article["attachment"]:
                continue

            attachment = (
                article["attachment"][0]
                if isinstance(article["attachment"], list)
                else article["attachment"]
            )  # NOTE: only the first document is necessary

            file_url = attachment.get("fileUrl")
            if not file_url:
                continue

            try:
                async with session.get(
                    file_url, headers=headers, timeout=30
                ) as response:
                    if response.status != 200:
                        log_message(f"Failed to fetch PDF: {file_url}", "ERROR")
                        continue

                    pdf_content = await response.read()
                    pdf_file = io.BytesIO(pdf_content)

                    all_text = extract_text(pdf_file)
                    if not all_text:
                        continue

            except Exception as e:
                log_message(f"Error fetching PDF {file_url}: {str(e)}", "ERROR")
                continue

            action_sections = re.split(r"Action to Take", all_text, flags=re.IGNORECASE)
            if len(action_sections) < 2:
                log_message(f"'Action to Take' not found: {file_url}", "WARNING")
                buy_recommendations.append(
                    {
                        "ticker": None,
                        "name": "---Empty---",
                        "actionDesc": "---Empty---",
                        "postDate": article.get("cfUpdatedAt"),
                        "url": article.get("slug"),
                        "file_url": file_url,
                        "subscription_name": subscription_name,
                    }
                )
                continue

            found_recommendation = False
            for section in action_sections[1:]:
                buy_match = re.search(r"\b(Buy|buying)\b", section, re.IGNORECASE)
                if not buy_match:
                    continue

                ticker_patterns = [
                    r"(NYSE|NASDAQ):\s*([A-Z]+)",
                    r"\(([A-Z]{2,5})\)",
                    r"([A-Z]{2,5})\s+(?:up to|\$)",
                ]

                ticker_match = None
                ticker_symbol = None

                for pattern in ticker_patterns:
                    ticker_match = re.search(pattern, section, re.IGNORECASE)
                    if ticker_match:
                        if "NYSE|NASDAQ" in pattern:
                            ticker_symbol = ticker_match.group(2)
                        else:
                            ticker_symbol = ticker_match.group(1)
                        break

                if (
                    buy_match
                    and ticker_match
                    and buy_match.start() < ticker_match.start()
                    and ticker_symbol
                ):

                    buy_recommendations.append(
                        {
                            "ticker": ticker_symbol,
                            "name": "---Empty---",
                            "actionDesc": "---Empty---",
                            "postDate": article.get("cfUpdatedAt"),
                            "url": article.get("slug"),
                            "file_url": file_url,
                            "subscription_name": subscription_name,
                        }
                    )
                    found_recommendation = True
                    break

            if not found_recommendation:
                buy_recommendations.append(
                    {
                        "ticker": None,
                        "name": "---Empty---",
                        "actionDesc": "---Empty---",
                        "postDate": article.get("cfUpdatedAt"),
                        "url": article.get("slug"),
                        "file_url": file_url,
                        "subscription_name": subscription_name,
                    }
                )

        except Exception as e:
            log_message(
                f"Error processing report article {article.get('slug', 'Unknown')}: {str(e)}",
                "ERROR",
            )
            continue

    return buy_recommendations


async def send_matches_to_telegram(buy_recs):
    for rec in buy_recs:
        ticker = rec["ticker"]

        if ticker is None:
            continue

        clean_ticker = re.match(r"^[A-Z]+", ticker)
        if clean_ticker:
            ticker = clean_ticker.group(0)

        name = rec["name"]
        actionDesc = rec["actionDesc"]
        postDate = rec["postDate"]
        sub_name = rec["subscription_name"].upper()
        url = f"https://my.paradigmpressgroup.com/article/{rec['url']}"
        file_url = rec["file_url"]

        post_time = datetime.fromisoformat(postDate.replace("Z", "+00:00"))

        post_time_us = post_time.astimezone(pytz.timezone("America/Chicago")).strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
        current_time_us = get_current_time().strftime("%Y-%m-%d %H:%M:%S %Z")

        message = f"<b>New Buy Recommendation - {sub_name}</b>\n\n"
        message += f"<b>Stock Symbol:</b> {ticker}\n"
        message += f"<b>Stock Name:</b> {name}\n"
        message += f"<b>Action Desc:</b> {actionDesc}\n"
        message += f"<b>URL:</b> {url}\n"
        if file_url:
            message += f"<b>Media Url:</b> {file_url}\n"

        message += f"<b>Post Time:</b> {post_time_us}\n"
        message += f"<b>Current Time:</b> {current_time_us}\n"

        await send_ws_message(
            {
                "name": f"Altucher - {sub_name}",
                "type": "Buy",
                "ticker": ticker,
                "sender": "altucher",
            },
        )
        await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
        log_message(
            f"Recommendations for `{sub_name}` with the ticker: `{ticker}` sent to Telegram and WebSocket: {ticker} - {url}",
            "INFO",
        )


async def process_subscription(session, subscription, proxy, processed_urls):
    articles = await fetch_articles(
        session,
        subscription["name"],
        subscription["id"],
        subscription["category"],
        proxy,
    )

    new_articles = [
        article
        for article in articles
        if article.get("slug") and article["slug"] not in processed_urls
    ]
    new_urls = {article["slug"] for article in articles if article.get("slug")}

    if new_articles:
        log_message(
            f"Found {len(new_articles)} new articles to process for {subscription['name']}.",
            "INFO",
        )

        # FIXME: remove this later when we properly handled drafts
        date = get_current_time().strftime("%Y_%m_%d_%H_%M_%S_%f")
        with open(f"data/delete_{date}.json", "w") as f:
            json.dump(new_articles, f, indent=2)

        if subscription["category"] == "Reports":
            buy_recs = await process_reports(
                session, new_articles, subscription["name"]
            )
        else:
            buy_recs = await process_articles(new_articles, subscription["name"])

        await send_matches_to_telegram(buy_recs)
        return new_urls
    return set()


async def run_scraper():
    processed_urls = load_processed_urls()
    proxies = load_proxies()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message(
                "Market is open. Starting to check for new articles...", "DEBUG"
            )
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                # Randomly select different proxies for each subscription
                available_proxies = proxies.copy()
                tasks = []

                for subscription in subscriptions:
                    if not available_proxies:
                        available_proxies = proxies.copy()
                    proxy = random.choice(available_proxies)
                    available_proxies.remove(proxy)

                    tasks.append(
                        process_subscription(
                            session, subscription, proxy, processed_urls
                        )
                    )

                new_urls_list = await asyncio.gather(*tasks)
                all_new_urls = set().union(*new_urls_list)

                if all_new_urls:
                    processed_urls.update(all_new_urls)
                    save_processed_urls(processed_urls)
                else:
                    log_message("No new articles found.", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([USERNAME, PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
