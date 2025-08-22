import asyncio
import json
import os
import random
import re
import sys
import time

import aiohttp
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

AJAX_URL = "https://banyanhill.com/wp-admin/admin-ajax.php"
CHECK_INTERVAL = 1
PROCESSED_IDS_FILE = "data/banyan_processed_ids.json"
TELEGRAM_BOT_TOKEN = os.getenv("BANYAN_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BANYAN_TRADE_ALERT_TELEGRAM_GRP")
PROXY_FILE = "cred/proxies.json"

subscriptions = [
    {"name": "Strategic Fortunes PRO", "term_id": "46767"},
    {"name": "Strategic Fortunes", "term_id": "21331"},
    {"name": "Extreme Fortunes", "term_id": "20315"},
]

os.makedirs("data", exist_ok=True)


def parse_ticker_from_title(title):
    """
    Extract the first ticker symbol from buy alerts only.
    Returns None if it's not a buy alert or no ticker found.
    """
    if not title:
        return None

    title_lower = title.lower()

    if not re.search(r"\b(buy|buying)\b", title_lower):
        return None

    if re.search(r"\b(sell|selling|take gains)\b", title_lower):
        return None

    ticker_patterns = [
        r"\((?:NYSE|NASDAQ|Nasdaq):\s*([A-Z]+)\)",
        r"\(([A-Z]{2,6})\)",
        r"\b(?:buy|buying)\s+(?:back\s+into\s+|into\s+)?([A-Z]{2,6})\b",
        r"\b(?:buy|buying)\s+([A-Z]{2,6})(?:\s*,|\s*&)",
    ]

    for pattern in ticker_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            ticker = match.group(1).upper()
            if ticker not in ["BACK", "INTO", "THESE", "NOW"]:
                return ticker

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


def load_processed_ids():
    try:
        with open(PROCESSED_IDS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_ids(ids):
    with open(PROCESSED_IDS_FILE, "w") as f:
        json.dump(list(ids), f, indent=2)
    log_message("Processed IDs saved.", "INFO")


async def fetch_articles(session, subscription_name, term_id, proxy, offset=0):
    try:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "dnt": "1",
            "origin": "https://banyanhill.com",
            "priority": "u=1, i",
            "referer": "https://banyanhill.com/next-wave-crypto-fortunes/trade-alerts/",
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

                            post_id = title_link.get("data-post-id")
                            title = title_link.get_text(strip=True)
                            url = title_link.get("href")

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

                            ticker = parse_ticker_from_title(title)

                            if post_id and title:
                                articles.append(
                                    {
                                        "post_id": post_id,
                                        "title": title,
                                        "url": url,
                                        "date": date_text,
                                        "description": description,
                                        "subscription_name": subscription_name,
                                        "ticker": ticker,  # Added ticker field
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


async def send_matches_to_telegram(trade_alerts):
    for alert in trade_alerts:
        title = alert["title"]
        description = alert["description"]
        date = alert["date"]
        url = alert["url"]
        sub_name = alert["subscription_name"]
        ticker = alert.get("ticker")

        current_time_us = get_current_time().strftime("%Y-%m-%d %H:%M:%S %Z")

        message = f"<b>New Alert - {sub_name}</b>\n\n"
        message += f"<b>Title:</b> {title}\n"

        if ticker:
            shorten_name = "".join(word[0].lower() for word in sub_name.split(" "))

            await send_ws_message(
                {
                    "name": f"Banyan - {shorten_name}",
                    "type": "Buy",
                    "ticker": ticker,
                    "sender": "banyan",
                    "target": "CSS",
                },
            )

            message += f"<b>Ticker:</b> {ticker}\n"

        message += f"<b>Current Time:</b> {current_time_us}\n"
        message += f"<b>Post Time:</b> {date}\n"
        message += f"<b>URL:</b> {url}\n"
        # NOTE: This just returns empty account login descirption so no need to send it now
        # message += f"<b>Description:</b> {description[:500]}{'...' if len(description) > 500 else ''}\n"

        await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)

        ticker_info = f" (Ticker: {ticker})" if ticker else ""
        log_message(
            f"Alert for `{sub_name}` sent to Telegram: {title[:50]}...{ticker_info} - {url}",
            "INFO",
        )


async def process_subscription(session, subscription, proxy, processed_ids):
    articles = await fetch_articles(
        session, subscription["name"], subscription["term_id"], proxy
    )

    new_articles = [
        article
        for article in articles
        if article.get("post_id") and article["post_id"] not in processed_ids
    ]
    new_ids = {article["post_id"] for article in articles if article.get("post_id")}

    if new_articles:
        buy_articles = [article for article in new_articles if article.get("ticker")]
        if buy_articles:
            ticker_list = [article["ticker"] for article in buy_articles]
            log_message(
                f"Found {len(new_articles)} new articles for {subscription['name']}, {len(buy_articles)} with tickers: {', '.join(ticker_list)}",
                "INFO",
            )
        else:
            log_message(
                f"Found {len(new_articles)} new articles for {subscription['name']}, no buy alerts with tickers.",
                "INFO",
            )

        date = get_current_time().strftime("%Y_%m_%d_%H_%M_%S_%f")
        with open(f"data/banyan_{date}.json", "w") as f:
            json.dump(new_articles, f, indent=2)

        await send_matches_to_telegram(new_articles)
        return new_ids
    return set()


async def run_scraper():
    processed_ids = load_processed_ids()
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

                available_proxies = proxies.copy()
                tasks = []

                for subscription in subscriptions:
                    if not available_proxies:
                        available_proxies = proxies.copy()
                    proxy = random.choice(available_proxies)
                    available_proxies.remove(proxy)

                    tasks.append(
                        process_subscription(
                            session, subscription, proxy, processed_ids
                        )
                    )

                new_ids_list = await asyncio.gather(*tasks)
                all_new_ids = set().union(*new_ids_list)

                if all_new_ids:
                    processed_ids.update(all_new_ids)
                    save_processed_ids(processed_ids)
                else:
                    log_message("No new articles found.", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
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
