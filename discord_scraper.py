import asyncio
import json
import os
import random
import sys

from dotenv import load_dotenv
from DrissionPage import ChromiumOptions, ChromiumPage

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.ticker_deck_sender import initialize_ticker_deck, send_ticker_deck_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()

DISCORD_LOGIN_URL = "https://discord.com/login"
CHANNELS = [
    "https://discord.com/channels/916525682887122974/919332311391154256",
    "https://discord.com/channels/916525682887122974/1217309136681832540",
]
CHECK_INTERVAL = 0.4
PROCESSED_MESSAGES_FILE = "data/discord_processed_messages.json"
SESSION_FILE = "data/discord_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("DISCORD_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("DISCORD_TELEGRAM_GRP")
DISCORD_EMAIL = os.getenv("DISCORD_EMAIL")
DISCORD_PASSWORD = os.getenv("DISCORD_PASSWORD")

os.makedirs("data", exist_ok=True)

co = ChromiumOptions()
page = ChromiumPage(co)
tab_ids = []


def load_processed_messages():
    try:
        with open(PROCESSED_MESSAGES_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_processed_messages(messages):
    with open(PROCESSED_MESSAGES_FILE, "w") as f:
        json.dump(messages, f, indent=2)
    log_message("Processed messages saved.", "INFO")


def initialize_tabs():
    global page, tab_ids

    if tab_ids:
        for tab_id in tab_ids[1:]:
            try:
                page.get_tab(tab_id).close()
            except Exception as e:
                log_message(f"Error closing tab: {e}", "WARNING")

    page.new_tab()
    tab_ids = page.tab_ids
    log_message(f"Initialized {len(tab_ids)} tabs", "INFO")

    page.activate_tab(tab_ids[0])
    return tab_ids


async def send_captcha_notification():
    message = f"<b>Discord Login Captcha Detected</b>\n\n"
    message += f"<b>Time:</b> {get_current_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
    message += f"<b>Action Required:</b> Manual login needed\n"
    message += f"<b>Status:</b> Bot waiting for manual intervention"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message("Captcha notification sent to Telegram", "WARNING")


async def login_discord():
    global page

    try:
        page.get(DISCORD_LOGIN_URL)
        await asyncio.sleep(2)

        if "discord.com/channels/@me" in page.url or "discord.com/app" in page.url:
            log_message("Successfully logged into Discord", "INFO")
            return True

        if page.ele("Email or Phone Number"):
            page.ele("Email or Phone Number").input(DISCORD_EMAIL)
            page.ele("Password").input(DISCORD_PASSWORD)
            page.ele("Log In").click()
            await asyncio.sleep(3)

            if (
                page.ele('div[class*="captcha"]')
                or page.ele('iframe[src*="captcha"]')
                or "captcha" in page.html.lower()
            ):
                await send_captcha_notification()
                log_message(
                    "Captcha detected, waiting for manual intervention...", "WARNING"
                )

                while (
                    page.ele('div[class*="captcha"]')
                    or page.ele('iframe[src*="captcha"]')
                    or "captcha" in page.html.lower()
                ):
                    await asyncio.sleep(10)

                log_message("Captcha resolved, continuing...", "INFO")

            await asyncio.sleep(5)

            if "discord.com/channels/@me" in page.url or "discord.com/app" in page.url:
                log_message("Successfully logged into Discord", "INFO")
                return True
            else:
                log_message("Login failed - not redirected to Discord app", "ERROR")
                await send_captcha_notification()
                return False

    except Exception as e:
        log_message(f"Error during Discord login: {e}", "ERROR")
        return False


def extract_channel_name(url):
    channel_id = url.split("/")[-1]
    channel_mapping = {
        "919332311391154256": "Yonezu",
        "1217309136681832540": "Mystic",
    }
    return channel_mapping.get(channel_id, f"Channel-{channel_id}")


async def random_scroll(current_tab):
    try:
        if random.random() < 0.1:
            scroll_amount = random.randint(-200, 200)
            current_tab.scroll(0, scroll_amount)
            await asyncio.sleep(0.1)
    except Exception as e:
        log_message(f"Error during random scroll: {e}", "DEBUG")


async def get_latest_message(tab_id, channel_url):
    try:
        page.activate_tab(tab_id)
        current_tab = page.get_tab(tab_id)

        if current_tab.url == channel_url:
            current_tab.get(channel_url)
            await asyncio.sleep(3)

        await random_scroll(current_tab)

        try:
            messages = current_tab.eles("@id:message-content")
            if not messages:
                return None

            content_elem = messages[-1]
            timestamp_elem = current_tab.ele("@id:message-timestamp")

            if "NoneElement" not in str(timestamp_elem) and "NoneElement" not in str(
                content_elem
            ):
                timestamp = timestamp_elem.attr("datetime")
                content = content_elem.text
                message_id = content_elem.attr("id")

                message_data = {
                    "timestamp": timestamp,
                    "message_id": message_id or "",
                    "content": content,
                    "channel_url": channel_url,
                }
                return message_data

        except Exception as e:
            log_message(f"Error parsing message: {e}", "DEBUG")
            return None

        log_message(f"Last message for {channel_url} isn't available", "WARNING")
        return None

    except Exception as e:
        log_message(f"Error getting messages from {channel_url}: {e}", "ERROR")
        return None


async def send_new_message_notification(message_data, channel_name):
    current_time = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    telegram_message = f"<b>New Discord Message</b>\n\n"
    telegram_message += f"<b>Channel:</b> {channel_name}\n"
    telegram_message += f"<b>Message Time:</b> {message_data['timestamp']}\n"
    telegram_message += f"<b>Current Time:</b> {current_time}\n"
    telegram_message += f"<b>Content:</b>\n{message_data['content']}\n"
    telegram_message += f"<b>Channel URL:</b> {message_data['channel_url']}"

    await send_ticker_deck_message(
        sender="discord",
        name=channel_name,
        content=f"New message in {channel_name}: {message_data['content']}",
    )
    await send_telegram_message(telegram_message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)

    log_message(f"New message notification sent for {channel_name}", "INFO")


async def setup_channel_tabs():
    global tab_ids

    for i, channel_url in enumerate(CHANNELS):
        page.activate_tab(tab_ids[i])
        page.get(channel_url)
        await asyncio.sleep(3)
        log_message(f"Tab {i} loaded with {extract_channel_name(channel_url)}", "INFO")


async def run_scraper():
    processed_messages = load_processed_messages()

    if not await login_discord():
        log_message("Failed to login to Discord", "CRITICAL")
        return

    initialize_tabs()
    await setup_channel_tabs()

    while True:
        await sleep_until_market_open()
        await initialize_ticker_deck("Discord Scraper")

        log_message("Market is open. Starting to monitor Discord channels...", "DEBUG")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()

            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                break

            for i, channel_url in enumerate(CHANNELS):
                channel_name = extract_channel_name(channel_url)
                message = await get_latest_message(tab_ids[i], channel_url)
                log_message(f"Fetched latest message for {channel_name}", "INFO")

                if not message:
                    continue

                channel_key = channel_url.split("/")[-1]
                if channel_key not in processed_messages:
                    processed_messages[channel_key] = []

                message_id = (
                    f"{message['timestamp']}_{hash(message['content'])}"
                    if message["message_id"] == ""
                    else message["message_id"]
                )

                if message_id not in processed_messages[channel_key]:
                    await send_new_message_notification(message, channel_name)
                    processed_messages[channel_key].append(message_id)

                    if len(processed_messages[channel_key]) > 100:
                        processed_messages[channel_key] = processed_messages[
                            channel_key
                        ][-50:]

                save_processed_messages(processed_messages)

            await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP, DISCORD_EMAIL, DISCORD_PASSWORD]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        page.quit()
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        page.quit()
        sys.exit(1)


if __name__ == "__main__":
    main()
