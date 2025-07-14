import asyncio
import difflib
import json
import os
import random
import sys

import websockets
from dotenv import load_dotenv
from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage.common import Keys

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()

WEBSOCKET_PORT = 8675
TWITTER_HOME_URL = "https://x.com/home"
REFRESH_INTERVAL = random.uniform(5, 10)
PROCESSED_POSTS_FILE = "data/twitter_processed_posts.json"
SESSION_FILE = "data/twitter_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("TWITTER_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("TWITTER_TELEGRAM_GRP")
TWITTER_USERNAME = os.getenv("TWITTER_USERNAME")
TWITTER_PASSWORD = os.getenv("TWITTER_PASSWORD")

os.makedirs("data", exist_ok=True)

co = ChromiumOptions()
page = ChromiumPage(co)
processed_data_global = {"posts": [], "last_post": None}


def text_similarity(text1, text2, threshold=0.95):
    similarity = difflib.SequenceMatcher(None, text1.lower(), text2.lower()).ratio()
    return similarity >= threshold


def find_matching_post(search_content, posts_list):
    for post in posts_list:
        if text_similarity(search_content, post["content"]):
            return post
    return None


async def handle_websocket_message(websocket):
    global processed_data_global

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                search_content = data.get("content", "")

                if not search_content:
                    await websocket.send(json.dumps({"error": "No content provided"}))
                    continue

                matching_post = find_matching_post(
                    search_content, processed_data_global["posts"]
                )

                if matching_post:
                    response = {
                        "found": True,
                        "username": matching_post["username"],
                        "content": matching_post["content"],
                        "source": "database",
                    }
                    await websocket.send(json.dumps(response))
                    continue

                # If not found, refresh and check again
                log_message("Post not found in DB, refreshing feed", "INFO")
                fresh_posts = await refresh_and_get_posts()

                if fresh_posts:
                    await store_new_posts(fresh_posts, processed_data_global)
                    matching_post = find_matching_post(search_content, fresh_posts)

                    if matching_post:
                        response = {
                            "found": True,
                            "username": matching_post["username"],
                            "content": matching_post["content"],
                            "source": "fresh_fetch",
                        }
                        await websocket.send(json.dumps(response))
                        continue

                # Still not found
                response = {"found": False, "message": "Post not found"}
                await websocket.send(json.dumps(response))

            except json.JSONDecodeError:
                await websocket.send(json.dumps({"error": "Invalid JSON"}))
            except Exception as e:
                log_message(f"WebSocket error: {e}", "ERROR")
                await websocket.send(json.dumps({"error": "Server error"}))

    except websockets.exceptions.ConnectionClosed:
        log_message("WebSocket connection closed", "INFO")


def load_processed_posts():
    global processed_data_global
    try:
        with open(PROCESSED_POSTS_FILE, "r") as f:
            processed_data_global = json.load(f)
            return processed_data_global
    except FileNotFoundError:
        processed_data_global = {"posts": [], "last_post": None}
        return processed_data_global


def save_processed_posts(posts_data):
    global processed_data_global
    with open(PROCESSED_POSTS_FILE, "w") as f:
        json.dump(posts_data, f, indent=2)
    processed_data_global = posts_data
    log_message("Processed posts saved.", "INFO")


async def send_captcha_notification():
    message = f"<b>Twitter Login Captcha Detected</b>\n\n"
    message += f"<b>Time:</b> {get_current_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
    message += f"<b>Action Required:</b> Manual login needed\n"
    message += f"<b>Status:</b> Bot waiting for manual intervention"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message("Captcha notification sent to Telegram", "WARNING")


def not_none_element(element):
    return False if "NoneElement" in str(element) else True


async def login_twitter():
    global page

    try:
        page.get(TWITTER_HOME_URL)
        await asyncio.sleep(3)

        if "x.com/home" in page.url:
            log_message("Already logged into Twitter", "INFO")
            return True

        username_input = page.ele('css:input[name="text"]')
        if not_none_element(username_input):
            username_input.input(TWITTER_USERNAME + Keys.ENTER)
            await asyncio.sleep(1)

        password_input = page.ele('css:input[name="password"]')
        if not_none_element(username_input):
            password_input.input(TWITTER_PASSWORD + Keys.ENTER)
            await asyncio.sleep(3)

        if (
            not_none_element(
                page.ele('css:div[data-testid="ocfEnterTextTextInput"]', timeout=2)
            )
            or not_none_element(
                page.ele('css:div[data-testid="challenge_response_input"]', timeout=2)
            )
            or "challenge" in page.html.lower()
        ):
            await send_captcha_notification()
            log_message(
                "Challenge detected, waiting for manual intervention...", "WARNING"
            )

            while (
                not_none_element(
                    page.ele('css:div[data-testid="ocfEnterTextTextInput"]', timeout=2)
                )
                or not_none_element(
                    page.ele(
                        'css:div[data-testid="challenge_response_input"]', timeout=2
                    )
                )
                or "challenge" in page.html.lower()
            ):
                await asyncio.sleep(10)

            log_message("Challenge resolved, continuing...", "INFO")

        await asyncio.sleep(5)

        if "x.com/home" in page.url:
            log_message("Successfully logged into Twitter", "INFO")
            return True
        else:
            page.get(TWITTER_HOME_URL)
            await asyncio.sleep(3)

            if "x.com/home" in page.url:
                log_message("Successfully logged into Twitter", "INFO")
                return True
            else:
                log_message("Login failed - not redirected to Twitter home", "ERROR")
                await send_captcha_notification()
                return False

    except Exception as e:
        log_message(f"Error during Twitter login: {e}", "ERROR")
        return False


def extract_posts():
    try:
        posts = []
        post_containers = page.eles(
            'css:section[aria-labelledby^="accessible-list-0"] > div > div > div'
        )

        for container in post_containers:
            try:
                username_elem = container.ele(
                    'css:div[data-testid="User-Name"] > div:nth-child(2) > div > div > a > div > span',
                    timeout=1,
                )
                tweet_elem = container.ele(
                    'css:div[data-testid="tweetText"]', timeout=1
                )

                analytics_elem = container.ele(
                    'css:[aria-label*="View post analytics"]', timeout=1
                )

                if (
                    not_none_element(username_elem)
                    and not_none_element(tweet_elem)
                    and not_none_element(analytics_elem)
                ):
                    username = username_elem.text
                    tweet_text = tweet_elem.text
                    tweet_id = (
                        analytics_elem.attr("href").split("/status/")[1].split("/")[0]
                    )

                    if username and tweet_text and tweet_id:
                        post_data = {
                            "username": username,
                            "content": tweet_text,
                            "timestamp": get_current_time().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "post_id": tweet_id,
                        }
                        posts.append(post_data)

            except Exception as e:
                log_message(f"Error parsing individual post: {e}", "DEBUG")
                continue

        return posts

    except Exception as e:
        log_message(f"Error extracting posts: {e}", "ERROR")
        return []


async def navigate_to_following():
    try:
        page.get(TWITTER_HOME_URL)
        await asyncio.sleep(3)

        # FIXME: fix this shit too it should propery log things
        page.ele("Following").click()
        log_message("I think we did navigate to following page", "WARNING")
        return True

    except Exception as e:
        log_message(f"Error navigating to following: {e}", "ERROR")
        return False


async def scroll_to_find_last_post(last_post_data):
    if not last_post_data:
        return True

    max_scrolls = 10
    scroll_count = 0

    while scroll_count < max_scrolls:
        posts = extract_posts()

        for post in posts:
            if (
                post["username"] == last_post_data["username"]
                and post["content"] == last_post_data["content"]
            ):
                log_message("Found last saved post", "INFO")
                return True

        page.scroll.down()
        await asyncio.sleep(1)
        scroll_count += 1

    log_message("Could not find last saved post after scrolling", "WARNING")
    return False


async def refresh_and_get_posts():
    try:
        log_message(f"Starting post refresh...")
        page.scroll.to_top()
        page.refresh()
        posts = extract_posts()
        log_message(f"Extracted {len(posts)} posts", "INFO")
        return posts

    except Exception as e:
        log_message(f"Error refreshing and getting posts: {e}", "ERROR")
        return []


async def store_new_posts(new_posts, processed_data):
    stored_count = 0

    existing_post_ids = {post["post_id"] for post in processed_data["posts"]}

    for post in new_posts:
        if post["post_id"] not in existing_post_ids:
            processed_data["posts"].append(post)
            stored_count += 1

    if stored_count > 0:
        processed_data["last_post"] = new_posts[0] if new_posts else None

        if len(processed_data["posts"]) > 1000:
            processed_data["posts"] = processed_data["posts"][-500:]

        save_processed_posts(processed_data)
        log_message(f"Stored {stored_count} new posts", "INFO")

    return stored_count


async def start_websocket_server():
    log_message(f"Starting WebSocket server on port {WEBSOCKET_PORT}", "INFO")
    return await websockets.serve(handle_websocket_message, "0.0.0.0", WEBSOCKET_PORT)


async def run_scraper():
    processed_data = load_processed_posts()
    failed_login_attempts = 0
    max_failed_attempts = 3

    while failed_login_attempts < max_failed_attempts:
        if await login_twitter():
            break
        else:
            failed_login_attempts += 1
            log_message(f"Login attempt {failed_login_attempts} failed", "ERROR")
            if failed_login_attempts < max_failed_attempts:
                await asyncio.sleep(60)  # Wait 1 minute before retrying

    if failed_login_attempts >= max_failed_attempts:
        log_message("Failed to login after maximum attempts", "CRITICAL")
        return

    if not await navigate_to_following():
        log_message("Failed to navigate to following page", "CRITICAL")
        return

    await scroll_to_find_last_post(processed_data.get("last_post"))

    refresh_count = 0
    consecutive_errors = 0
    max_consecutive_errors = 5

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to monitor Twitter following...", "DEBUG")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()

            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                break

            try:
                if refresh_count % 10 == 0:
                    log_message("Re-navigating to following feed", "INFO")
                    if not await navigate_to_following():
                        log_message(
                            "Failed to re-navigate to following, continuing...",
                            "WARNING",
                        )

                posts = await refresh_and_get_posts()

                if posts:
                    stored_count = await store_new_posts(posts, processed_data)
                    if stored_count > 0:
                        log_message(
                            f"Total posts in storage: {len(processed_data['posts'])}",
                            "INFO",
                        )
                    consecutive_errors = 0  # Reset error counter on success
                else:
                    consecutive_errors += 1

                if consecutive_errors >= max_consecutive_errors:
                    log_message(
                        f"Too many consecutive errors ({consecutive_errors}), attempting re-login",
                        "WARNING",
                    )
                    if await login_twitter():
                        await navigate_to_following()
                        consecutive_errors = 0
                    else:
                        log_message("Re-login failed, continuing with errors", "ERROR")

                refresh_count += 1
                refresh_delay = random.uniform(5, 10)
                await asyncio.sleep(refresh_delay)

            except Exception as e:
                log_message(f"Error in main loop: {e}", "ERROR")
                consecutive_errors += 1
                await asyncio.sleep(30)  # Wait longer on errors


async def main_async():
    websocket_server = await start_websocket_server()

    try:
        await asyncio.gather(run_scraper(), websocket_server.wait_closed())
    except Exception as e:
        log_message(f"Error in main async: {e}", "CRITICAL")
        websocket_server.close()
        await websocket_server.wait_closed()
        page.quit()


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP, TWITTER_USERNAME, TWITTER_PASSWORD]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
        page.quit()
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        page.quit()
        sys.exit(1)


if __name__ == "__main__":
    main()
