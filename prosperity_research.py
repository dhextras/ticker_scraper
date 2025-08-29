import asyncio
import json
import os
import re
import sys
import time
from urllib.parse import quote

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

CHECK_INTERVAL = 1
PROCESSED_SLUGS_FILE = "data/prosperity_processed_slugs.json"
TELEGRAM_BOT_TOKEN = os.getenv("PROSPERITY_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("PROSPERITY_TRADE_ALERT_TELEGRAM_GRP")

subscriptions = [
    {
        "name": "The American Prosperity Report",
        "tag": "The American Prosperity Report (Alerts)",
        "short_name": "tapr",
    },
    {
        "name": "8-Figure Fortunes",
        "tag": "8-Figure Fortunes (Alerts)",
        "short_name": "8ff",
    },
    {
        "name": "Profit Accelerator",
        "tag": "Profit Accelerator (Alerts)",
        "short_name": "pa",
    },
    {
        "name": "Microcap Fortunes",
        "tag": "Microcap Fortunes (Alerts)",
        "short_name": "mcf",
    },
]

os.makedirs("data", exist_ok=True)


def extract_buy_tickers(action_text):
    if not action_text or action_text.upper() == "NONE":
        return []

    tickers = []

    sections = action_text.split("|")

    for section in sections:
        section = section.strip()

        buy_matches = re.finditer(
            r"\bBUY\s+([^(]+)\s*\(([A-Z]+)\)", section, re.IGNORECASE
        )
        for match in buy_matches:
            ticker = match.group(2).strip()
            if ticker:
                tickers.append(ticker)

        buy_matches = re.finditer(
            r"\bBuy\s+([^(]+)\s*\(([A-Z]+)\)", section, re.IGNORECASE
        )
        for match in buy_matches:
            ticker = match.group(2).strip()
            if ticker:
                tickers.append(ticker)

    return list(set(tickers))


def load_processed_slugs():
    try:
        with open(PROCESSED_SLUGS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_slugs(slugs):
    with open(PROCESSED_SLUGS_FILE, "w") as f:
        json.dump(list(slugs), f, indent=2)
    log_message("Processed slugs saved.", "INFO")


async def fetch_posts(session):
    tags = [sub["tag"] for sub in subscriptions]
    encoded_tags = ",".join([quote(tag) for tag in tags])
    url = f"https://prosperityresearch.com/posts?page=1&per_page=4&tags={encoded_tags}&_data=routes%2F__loaders%2Fposts"

    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    }

    try:
        start_time = time.time()
        async with session.get(url, headers=headers, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                log_message(
                    f"Fetching posts took {(time.time() - start_time):.2f}s",
                    "INFO",
                )
                return data.get("posts", [])
            else:
                log_message(
                    f"Failed to fetch posts HTTP: {response.status}",
                    "ERROR",
                )
                return []
    except asyncio.TimeoutError:
        log_message(f"Timeout fetching posts", "WARNING")
        return []
    except Exception as e:
        log_message(f"Error fetching posts: {e}", "ERROR")
        return []


def extract_remix_context(html_content):
    soup = BeautifulSoup(html_content, "html.parser")

    for script in soup.find_all("script"):
        if script.string and "window.__remixContext" in script.string:
            match = re.search(
                r"window\.__remixContext\s*=\s*({.*?});", script.string, re.DOTALL
            )
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue
    return None


def extract_text_from_content(content_item):
    text_parts = []

    if isinstance(content_item, dict):
        if content_item.get("type") == "text" and "text" in content_item:
            text_parts.append(content_item["text"])

        if "content" in content_item:
            content = content_item["content"]
            if isinstance(content, list):
                for item in content:
                    text_parts.append(extract_text_from_content(item))
            elif isinstance(content, dict):
                text_parts.append(extract_text_from_content(content))

    elif isinstance(content_item, list):
        for item in content_item:
            text_parts.append(extract_text_from_content(item))

    return " ".join(text_parts).strip()


def find_action_to_take(content_list):
    action_texts = []

    for i, item in enumerate(content_list):
        if not isinstance(item, dict) or "type" not in item:
            continue

        current_text = extract_text_from_content(item)

        if re.search(r"action\s+to\s+take\s*:?", current_text, re.IGNORECASE):
            action_match = re.search(
                r"action\s+to\s+take\s*:\s*(.+)", current_text, re.IGNORECASE
            )
            if action_match and action_match.group(1).strip():
                complete_action = action_match.group(1).strip()
                if complete_action.upper() not in ["", "NONE"]:
                    action_texts.append(complete_action)
                    continue
                elif complete_action.upper() == "NONE":
                    return "NONE"

            if re.match(
                r"action\s+to\s+take\s*:?\s*$", current_text.strip(), re.IGNORECASE
            ):
                for j in range(i + 1, min(i + 10, len(content_list))):
                    next_item = content_list[j]
                    if not isinstance(next_item, dict):
                        continue

                    next_text = extract_text_from_content(next_item).strip()

                    if not next_text or re.search(
                        r"(regards|sincerely|to view|if you have|questions)",
                        next_text,
                        re.IGNORECASE,
                    ):
                        break

                    if re.search(r"\b(buy|sell)\b", next_text, re.IGNORECASE):
                        action_texts.append(next_text)
                    elif len(action_texts) > 0:
                        break

                break

    if action_texts:
        return " | ".join(action_texts)

    return "Not found"


def extract_images(content_list):
    images = []

    def extract_images_recursive(content):
        if isinstance(content, dict):
            if content.get("type") == "imageBlock":
                attrs = content.get("attrs", {})
                src = attrs.get("src")
                if src:
                    images.append(src)

            if "content" in content:
                extract_images_recursive(content["content"])

        elif isinstance(content, list):
            for item in content:
                extract_images_recursive(item)

    for item in content_list:
        extract_images_recursive(item)

    return images


async def fetch_post_content(session, slug):
    url = f"https://prosperityresearch.com/p/{slug}"

    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "max-age=0",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    }

    try:
        async with session.get(url, headers=headers, timeout=10) as response:
            if response.status == 200:
                html_content = await response.text()
            else:
                return {"content_available": False, "error": f"HTTP {response.status}"}
    except Exception as e:
        return {"content_available": False, "error": f"Failed to fetch page: {e}"}

    remix_context = extract_remix_context(html_content)

    if not remix_context:
        return {"content_available": False, "error": "Failed to extract remix context"}

    try:
        loader_data = remix_context["state"]["loaderData"]
        route_key = f"routes/p/$slug"

        if route_key not in loader_data:
            return {"content_available": False, "error": f"Route {route_key} not found"}

        page_data = loader_data[route_key]["page"]["viewable_page_version"]["content"][
            "content"
        ][0]["content"][1]["attrs"]["data"]["content"]

        images = extract_images(page_data)
        action_to_take = find_action_to_take(page_data)

        return {
            "content_available": True,
            "images": images,
            "action_to_take": action_to_take,
        }

    except (KeyError, IndexError, TypeError) as e:
        return {
            "content_available": False,
            "error": f"Page content structure not available: {str(e)}",
        }


def get_subscription_info(content_tags):
    for sub in subscriptions:
        if sub["tag"] in content_tags:
            return sub["name"], sub["short_name"]
    return "Prosperity Research", "pr"


async def send_alerts(post_data):
    title = post_data["web_title"]
    created_at = post_data["created_at"]
    url = post_data["url"]
    content_tags = post_data.get("content_tags", [])
    action_to_take = post_data.get("action_to_take", "Not found")
    images = post_data.get("images", [])
    content_available = post_data.get("content_available", False)

    sub_name, short_name = get_subscription_info(content_tags)
    tickers = extract_buy_tickers(action_to_take) if content_available else []

    if tickers:
        for ticker in tickers:
            await send_ws_message(
                {
                    "name": f"Prosperity - {short_name}",
                    "type": "Buy",
                    "ticker": ticker,
                    "sender": "prosperity",
                },
            )

        log_message(
            f"Sent {len(tickers)} tickers to websocket: {', '.join(tickers)} from {sub_name}",
            "INFO",
        )

    current_time_us = get_current_time().strftime("%Y-%m-%d %H:%M:%S %Z")

    message = f"<b>New Alert - {sub_name}</b>\n\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Current Time:</b> {current_time_us}\n"
    message += f"<b>Post Time:</b> {created_at}\n"
    message += f"<b>URL:</b> {url}\n"

    if content_available:
        if tickers:
            message += f"<b>Tickers:</b> {', '.join(tickers)}\n"
        if images:
            message += f"<b>Images:</b> {len(images)} found\n"
            for img in images:
                message += f"  - {img}\n"

        message += f"<b>Action to Take:</b> {action_to_take}\n"

    else:
        message += f"<b>Content Status:</b> Not available - {post_data.get('error', 'Unknown error')}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)

    ticker_info = f" (Tickers: {', '.join(tickers)})" if tickers else ""
    log_message(
        f"Alert for `{sub_name}` sent to Telegram: {title[:50]}...{ticker_info} - {url}",
        "INFO",
    )


async def process_posts(session, processed_slugs):
    posts = await fetch_posts(session)

    if not posts:
        return set()

    new_posts = [
        post
        for post in posts
        if post.get("slug") and post["slug"] not in processed_slugs
    ]
    new_slugs = {post["slug"] for post in posts if post.get("slug")}

    if new_posts:
        log_message(f"Found {len(new_posts)} new posts", "INFO")

        date = get_current_time().strftime("%Y_%m_%d_%H_%M_%S_%f")

        for i, post in enumerate(new_posts):
            post_info = {
                "web_title": post.get("web_title", ""),
                "created_at": post.get(
                    "override_scheduled_at", post.get("created_at", "")
                ),
                "slug": post.get("slug", ""),
                "url": f"https://prosperityresearch.com/p/{post.get('slug', '')}",
                "content_tags": [],
            }

            if "content_tags" in post and isinstance(post["content_tags"], list):
                post_info["content_tags"] = [
                    tag.get("display", "")
                    for tag in post["content_tags"]
                    if "display" in tag
                ]

            content_data = await fetch_post_content(session, post.get("slug", ""))
            post_info.update(content_data)

            with open(f"data/prosperity_{date}_{i+1}.json", "w") as f:
                json.dump(post_info, f, indent=2)

            await send_alerts(post_info)

            if i < len(new_posts) - 1:
                await asyncio.sleep(CHECK_INTERVAL)

        return new_slugs

    return set()


async def run_scraper():
    processed_slugs = load_processed_slugs()

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message("Market is open. Starting to check for new posts...", "DEBUG")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()
                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                new_slugs = await process_posts(session, processed_slugs)

                if new_slugs:
                    processed_slugs.update(new_slugs)
                    save_processed_slugs(processed_slugs)
                else:
                    log_message("No new posts found.", "INFO")

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
