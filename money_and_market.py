import asyncio
import json
import os
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
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

AJAX_URL = "https://moneyandmarkets.com/wp-admin/admin-ajax.php"
LOGIN_URL = "https://moneyandmarkets.com/wp-login.php"
USERNAME = os.getenv("MONEYANDMARKETS_USERNAME")
PASSWORD = os.getenv("MONEYANDMARKETS_PASSWORD")
CHECK_INTERVAL = 1
PROCESSED_URLS_FILE = "data/moneyandmarkets_processed_urls.json"
TELEGRAM_BOT_TOKEN = os.getenv("MONEYANDMARKETS_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("MONEYANDMARKETS_TELEGRAM_GRP")
PROXY_FILE = "cred/proxies.json"

subscriptions = [
    {"name": "10x Stocks", "term_id": "48578", "short_name": "TS"},
    {"name": "Apex Alerts", "term_id": "49118", "short_name": "AA"},
    {"name": "Green Zone Fortunes Pro", "term_id": "48847", "short_name": "GZFP"},
    {"name": "Infinite Momentum Alert", "term_id": "48708", "short_name": "IMA"},
]

os.makedirs("data", exist_ok=True)


def login_sync(session: requests.Session) -> bool:
    """Login to Money and Markets using requests session"""
    try:
        payload = {"log": USERNAME, "pwd": PASSWORD}
        response = session.post(LOGIN_URL, data=payload)
        if response.status_code == 200:
            log_message("Money and Markets login successful", "INFO")
            return True
        else:
            log_message(
                f"Money and Markets login failed: HTTP {response.status_code}", "ERROR"
            )
            return False
    except Exception as e:
        log_message(f"Error during Money and Markets login: {e}", "ERROR")
        return False


def get_headers() -> Dict[str, str]:
    """Get headers for requests"""
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def extract_action_details(content: str) -> Optional[Tuple[str, str, str]]:
    """
    Extract ticker, action (buy/sell) from 'Action to Take' sections
    Returns: (ticker, action, exchange) or None
    """
    try:
        # Find all "Action to Take" sections (case insensitive)
        action_sections = re.split(
            r"Action\s+to\s+Take\s*:", content, flags=re.IGNORECASE
        )

        if len(action_sections) < 2:
            return None

        for section in action_sections[1:]:
            action_match = re.search(r"^\s*(Buy|Sell)", section.strip(), re.IGNORECASE)
            if not action_match:
                continue

            action = action_match.group(1).lower().capitalize()

            # Look for ticker in parentheses:
            # (ABCD), (Nasdaq: ABCD), (NYSE: ABC), etc..
            ticker_patterns = [
                r"\(\s*(?:NYSE|NASDAQ|Nasdaq|Nyse)\s*:\s*([A-Z]{1,6})\s*\)",
                r"\(\s*([A-Z]{1,6})\s*\)",
            ]

            for pattern in ticker_patterns:
                ticker_match = re.search(pattern, section)
                if ticker_match:
                    ticker = ticker_match.group(1).upper()

                    exchange = ""
                    exchange_match = re.search(
                        r"\(\s*(NYSE|NASDAQ|Nasdaq|Nyse)\s*:\s*[A-Z]{1,6}\s*\)",
                        section,
                        re.IGNORECASE,
                    )
                    if exchange_match:
                        exchange = exchange_match.group(1).upper()

                    # Verify it ends with "at the market" or "at the open"
                    if re.search(r"at\s+the\s+(market|open)", section, re.IGNORECASE):
                        log_message(
                            f"Found action: {action} {ticker} ({exchange})", "INFO"
                        )
                        return (ticker, action, exchange)

        return None

    except Exception as e:
        log_message(f"Error extracting action details: {e}", "ERROR")
        return None


async def fetch_page_content(
    session: requests.Session, url: str
) -> Optional[Tuple[str, str, str, float]]:
    """
    Fetch page content and extract ticker/action information
    Returns: (ticker, exchange, action, fetch_time) or None
    """
    try:
        start_time = time.time()
        response = await asyncio.to_thread(
            session.get, url, headers=get_headers(), timeout=15
        )
        fetch_time = time.time() - start_time

        if response.status_code == 200:
            content = response.text
            soup = BeautifulSoup(content, "html.parser")
            all_text = soup.get_text(separator=" ", strip=True)

            action_details = extract_action_details(all_text)
            if action_details:
                ticker, action, exchange = action_details
                log_message(
                    f"Successfully extracted from {url}: {action} {ticker} ({exchange})",
                    "INFO",
                )
                return (ticker, exchange, action, fetch_time)
            else:
                log_message(f"No valid action found in {url}", "WARNING")

        else:
            log_message(f"Failed to fetch page: HTTP {response.status_code}", "ERROR")

    except Exception as e:
        log_message(f"Error fetching page content from {url}: {e}", "ERROR")

    return None


def load_proxies():
    try:
        with open(PROXY_FILE, "r") as f:
            data = json.load(f)
            # FIXME: Im lazy as fuck, so just copy the same proxies later sometime
            proxies = data.get("investor_place", [])
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


async def fetch_articles(session, subscription_name, term_id, proxy, offset=0):
    try:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "dnt": "1",
            "origin": "https://moneyandmarkets.com",
            "priority": "u=1, i",
            "referer": "https://moneyandmarkets.com/trade-alerts/",
            "sec-ch-ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
        }

        data = {
            "offset": str(offset),
            "term_id": term_id,
            "action": "load_more_archives",
        }

        proxy_url = f"http://{proxy}" if proxy else None

        start_time = time.time()
        async with session.post(
            AJAX_URL, headers=headers, data=data, proxy=proxy_url, timeout=2
        ) as response:
            if response.status == 200:
                try:
                    response_text = await response.text()
                    soup = BeautifulSoup(response_text, "html.parser")

                    articles = []
                    archive_items = soup.find_all("div", class_="archive_item")

                    log_message(
                        f"Fetched {len(archive_items)} {subscription_name} articles using proxy {proxy}, Took {(time.time() - start_time):.2f}s",
                        "INFO",
                    )

                    for item in archive_items:
                        try:
                            title_link = item.find("h2").find("a")
                            if not title_link:
                                continue

                            title = title_link.get_text(strip=True)
                            url = title_link.get("href")

                            if not url:
                                continue

                            date_span = item.find("span", class_="archive_date")
                            date_text = (
                                date_span.get_text(strip=True) if date_span else ""
                            )

                            description_p = item.find("p")
                            description = ""
                            if description_p:
                                read_more_link = description_p.find(
                                    "a", class_="readMore"
                                )
                                if read_more_link:
                                    read_more_link.decompose()
                                description = description_p.get_text(strip=True)

                            if title and url:
                                articles.append(
                                    {
                                        "title": title,
                                        "url": url,
                                        "date": date_text,
                                        "description": description,
                                        "subscription_name": subscription_name,
                                    }
                                )

                        except Exception as e:
                            log_message(f"Error parsing article item: {e}", "WARNING")
                            continue

                    return articles
                except Exception as e:
                    log_message(
                        f"Failed to parse data for {subscription_name} with proxy: {proxy}. Error: {e}",
                        "ERROR",
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
            f"Took more than 2 sec to fetch {subscription_name} with proxy: {proxy}",
            "WARNING",
        )
        return []
    except Exception as e:
        log_message(
            f"Error fetching {subscription_name} articles with proxy {proxy}: {e}",
            "ERROR",
        )
        return []


async def send_matches_to_telegram(
    url: str,
    title: str,
    ticker: str,
    exchange: str,
    action: str,
    sub_name: str,
    short_name: str,
    date: str,
    fetch_time: float,
) -> None:
    """Send ticker match to Telegram and WebSocket"""

    await send_ws_message(
        {
            "name": f"MoneyMarkets - {short_name}",
            "type": action,
            "ticker": ticker,
            "sender": "moneyandmarkets",
        },
    )

    current_time_us = get_current_time().strftime("%Y-%m-%d %H:%M:%S %Z")

    message = f"<b>New Stock Match - {sub_name}</b>\n\n"
    message += f"<b>Current Time:</b> {current_time_us}\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Stock Symbol:</b> {exchange}:{ticker}\n"
    message += f"<b>Action:</b> {action}\n"
    message += f"<b>Post Time:</b> {date}\n"
    message += f"<b>Article Fetch Time:</b> {fetch_time:.2f}s\n"
    message += f"<b>URL:</b> {url}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"Match sent to Telegram: {exchange}:{ticker} ({action}) - {url}", "INFO"
    )


async def process_new_articles(
    requests_session: requests.Session, new_articles: List[Dict[str, Any]]
) -> None:
    """Process new articles one by one with 1 second sleep between each"""

    if not new_articles:
        return

    log_message(
        f"Processing {len(new_articles)} new articles for content extraction...", "INFO"
    )

    for i, article in enumerate(new_articles):
        url = article["url"]
        title = article["title"]
        sub_name = article["subscription_name"]
        short_name = article["short_name"]
        date = article["date"]

        log_message(
            f"Processing article {i+1}/{len(new_articles)}: {title[:50]}...", "INFO"
        )

        result = await fetch_page_content(requests_session, url)

        if result:
            ticker, exchange, action, fetch_time = result
            await send_matches_to_telegram(
                url,
                title,
                ticker,
                exchange,
                action,
                sub_name,
                short_name,
                date,
                fetch_time,
            )
        else:
            log_message(f"No actionable content found in: {title[:50]}...", "WARNING")

        if i < len(new_articles) - 1:
            await asyncio.sleep(1)


async def process_subscription(
    aio_session, requests_session, subscription, proxy, processed_urls
):
    articles = await fetch_articles(
        aio_session, subscription["name"], subscription["term_id"], proxy
    )

    # Add short_name to each article
    for article in articles:
        article["short_name"] = subscription["short_name"]

    new_articles = [
        article
        for article in articles
        if article.get("url") and article["url"] not in processed_urls
    ]
    new_urls = {article["url"] for article in articles if article.get("url")}

    if new_articles:
        log_message(
            f"Found {len(new_articles)} new articles for {subscription['name']}",
            "INFO",
        )

        date = get_current_time().strftime("%Y_%m_%d_%H_%M_%S_%f")
        with open(f"data/moneyandmarkets_{date}.json", "w") as f:
            json.dump(new_articles, f, indent=2)

        await process_new_articles(requests_session, new_articles)

        return new_urls
    return set()


async def run_scraper():
    processed_urls = load_processed_urls()
    proxies = load_proxies()

    requests_session = requests.Session()
    if not login_sync(requests_session):
        log_message("Failed to login to Money and Markets", "CRITICAL")
        return

    async with aiohttp.ClientSession() as aio_session:
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

                available_proxies = proxies.copy()
                tasks = []

                for subscription in subscriptions:
                    if not available_proxies:
                        available_proxies = proxies.copy()
                    proxy = random.choice(available_proxies)
                    available_proxies.remove(proxy)

                    tasks.append(
                        process_subscription(
                            aio_session,
                            requests_session,
                            subscription,
                            proxy,
                            processed_urls,
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
