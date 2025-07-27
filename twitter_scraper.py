import asyncio
import datetime
import difflib
import hashlib
import json
import os
import random
import socket
import sys
import threading
import time

import websockets
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
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

WEBSOCKET_PORT = 8765
TCP_HOST = os.getenv("TCP_HOST")
TCP_PORT = int(os.getenv("TCP_PORT", 3005))
TCP_SECRET = os.getenv("TCP_SECRET")
TCP_USERNAME = os.getenv("TCP_USERNAME")
TWITTER_HOME_URL = "https://x.com/home"
REFRESH_INTERVAL = random.uniform(5, 10)
PROCESSED_POSTS_FILE = "data/twitter_processed_posts.json"
SESSION_FILE = "data/twitter_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("TWITTER_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("TWITTER_TELEGRAM_GRP")
TWITTER_USERNAME = os.getenv("TWITTER_USERNAME")
TWITTER_PASSWORD = os.getenv("TWITTER_PASSWORD")

os.makedirs("data", exist_ok=True)

tcp_client = None
co = ChromiumOptions()
page = ChromiumPage(co)
processed_data_global = {"posts": [], "last_post": None}


class EncryptedTcpClient:
    def __init__(self, server_ip, server_port, shared_secret, username):
        self.server_ip = server_ip
        self.server_port = server_port
        self.shared_secret = shared_secret
        self.username = username
        self.sock = None
        self.key = None
        self.connected = False
        self.thread = None
        self.message_queue = []
        self.lock = threading.Lock()

    def _get_utc_date(self):
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    def _derive_key(self):
        combined = self.shared_secret + self._get_utc_date()
        return hashlib.sha256(combined.encode("utf-8")).digest()

    def _encrypt(self, plaintext: str) -> bytes:
        iv = b"\x00" * 16
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        padded = pad(plaintext.encode("utf-8"), AES.block_size)
        return cipher.encrypt(padded)

    def connect(self):
        """
        Connects to the server, performs authentication (IV + ciphertext),
        starts receiver and heartbeat threads.
        """
        while not self.connected:  # Keep trying to connect if disconnected
            try:
                # Establish TCP connection
                self.sock = socket.create_connection((self.server_ip, self.server_port))
                self.connected = True
                log_message(
                    f"[TCP] Connected to {self.server_ip}:{self.server_port}", "INFO"
                )

                # Derive fresh key daily
                self.key = self._derive_key()

                # 1) Send authentication payload (IV + encrypted username)
                iv = b"\x00" * 16
                encrypted_username = self._encrypt(self.username)
                self.sock.sendall(iv + encrypted_username)
                log_message(
                    f"[TCP] Sent encrypted auth for username '{self.username}'", "INFO"
                )

                # 2) Start background threads
                threading.Thread(target=self._receive_loop, daemon=True).start()
                threading.Thread(target=self._heartbeat_loop, daemon=True).start()
                threading.Thread(target=self._message_processor, daemon=True).start()

                # 3) Send initial hello after a short pause
                time.sleep(1)
                self.send_message("Hello, server!")

            except Exception as e:
                log_message(f"[TCP] Connection error: {e}", "ERROR")
                self.connected = False
                log_message("[TCP] Attempting to reconnect...", "WARNING")

                # Sleep before reconnecting
                time.sleep(5)

    def send_message(self, message):
        """
        Public method: queues a message to be encrypted and sent to the server.
        Can accept string or dict (which will be converted to JSON).
        """
        with self.lock:
            self.message_queue.append(message)

    def _message_processor(self):
        """Process messages from queue and send them to the server."""
        while True:
            if self.message_queue and self.connected:
                with self.lock:
                    message = self.message_queue.pop(0)

                try:
                    # Convert dict to JSON if needed
                    if isinstance(message, dict):
                        message = json.dumps(message)

                    # Send message
                    self.sock.sendall((f"{message}<END>").encode("utf-8"))
                    if "heartbeat" not in message.lower():
                        log_message(f"[TCP] Sent message: {message}", "INFO")
                except Exception as e:
                    log_message(f"[TCP] Send error: {e}", "ERROR")
                    self.connected = False
                    self.reconnect()

            time.sleep(0.1)  # Small delay to prevent CPU hogging

    def _receive_loop(self):
        while self.connected:
            try:
                data = self.sock.recv(4096)
                if not data:
                    log_message("[TCP] Server disconnected", "WARNING")
                    self.connected = False
                    self.reconnect()
                    break

                text = data.decode("utf-8", errors="ignore")
                log_message(f"[TCP] Received: {text}", "INFO")
            except Exception as e:
                log_message(f"[TCP] Receive error: {e}", "ERROR")
                self.connected = False
                self.reconnect()

    def _heartbeat_loop(self):
        while self.connected:
            time.sleep(60)  # Send heartbeat every 60 seconds
            try:
                self.send_message(f"HEARTBEAT")
            except Exception as e:
                log_message(f"[TCP] Heartbeat error: {e}", "ERROR")
                self.connected = False
                self.reconnect()

    def reconnect(self):
        """Attempt to reconnect to the server"""
        log_message("[TCP] Reconnecting...", "WARNING")
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

        # Wait a moment before reconnecting
        time.sleep(5)
        self.connect()

    def start(self):
        """Start the TCP client in a separate thread."""
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.connect, daemon=True)
            self.thread.start()
            log_message("[TCP] Client thread started", "INFO")
        else:
            log_message("[TCP] Client thread already running", "INFO")


async def send_alert(msg: str):
    alert = f"🚨 ALERT: {msg}\n\nPlease check the server!"
    await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


def text_similarity(text1, text2, threshold=0.95):
    similarity = difflib.SequenceMatcher(None, text1.lower(), text2.lower()).ratio()
    return similarity >= threshold


def find_matching_post(search_content, posts_list):
    for post in posts_list:
        if text_similarity(search_content, post["content"]):
            return post
    return None


async def send_found_post(data, source):
    global tcp_client

    if tcp_client and tcp_client.connected:
        tcp_client.send_message(data)
    else:
        await send_alert("<b>TCP_CLIENT isn't Connected</b>")

    message = f"<b>New Post sender found - {source}</b>\n\n"
    message += f"<b>Sender:</b> {data['t']}\n"
    message += f"<b>Content:</b> {data['te'][:600]}{'\n\ncontent is trimmed.....' if len(data['te']) > 600 else ''}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def handle_websocket_message(websocket):
    global processed_data_global

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                search_content = data.get("content", "")

                if not search_content:
                    await send_alert(
                        f"<b>Couldn't found post</b>\n\n<b>Reason:</b> Search content was not provided"
                    )
                    log_message(f"Search content was empty content: {data}")
                    continue

                log_message(
                    f"Recevied a search for content: {search_content[:300]}...." "INFO"
                )
                matching_post = find_matching_post(
                    search_content, processed_data_global["posts"]
                )

                if matching_post:
                    # FIXME: Send to tcp and also after that send to telegram as well and when error shows up like not found or something like that send to telegram twitter channel as well
                    response_data = {
                        "pn": "x.com",
                        "t": matching_post["username"],
                        "te": matching_post["content"],
                        "ts": time.time() * 1000,
                    }
                    await send_found_post(response_data, "database")
                    continue

                # If not found, refresh and check again
                log_message("Post not found in DB, refreshing feed", "INFO")
                fresh_posts = await refresh_and_get_posts()

                if fresh_posts:
                    await store_new_posts(fresh_posts, processed_data_global)
                    matching_post = find_matching_post(search_content, fresh_posts)

                    if matching_post:
                        response_data = {
                            "pn": "x.com",
                            "t": matching_post["username"],
                            "te": matching_post["content"],
                            "ts": time.time() * 1000,
                        }
                        await send_found_post(response_data, "fresh_fetch")
                        continue

                await send_alert(
                    f"<b>Couldn't found post</b>\n\n<b>Reason:</b> Couldn't find the post in DB/Fresh fetch\n<b>Content</b>: {search_content[:200]}\n\n content is trimmed....."
                )

            except json.JSONDecodeError:
                await send_alert(
                    f"<b>Could'nt found post</b>\n\n<b>Reason:</b> Server Error check the logs"
                )
            except Exception as e:
                await send_alert(
                    f"<b>Could'nt found post</b>\n\n<b>Reason:</b> Server Error check the logs"
                )
                log_message(f"WebSocket error: {e}", "ERROR")

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

        # FIXME: Not sure if this the actual id to check the captchas so confirm it when we got hit with it
        if not_none_element(
            page.ele('css:div[data-testid="ocfEnterTextTextInput"]', timeout=2)
        ) or not_none_element(
            page.ele('css:div[data-testid="challenge_response_input"]', timeout=2)
        ):
            await send_captcha_notification()
            log_message(
                "Challenge detected, waiting for manual intervention...", "WARNING"
            )

            while not_none_element(
                page.ele('css:div[data-testid="ocfEnterTextTextInput"]', timeout=2)
            ) or not_none_element(
                page.ele('css:div[data-testid="challenge_response_input"]', timeout=2)
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
        # NOTE: Wait till the first username loads this way we can make sure that the content is loaded
        page.ele(
            'css:div[aria-label="Timeline: Your Home Timeline"] > div > div div[data-testid="User-Name"] > div:nth-child(1) > div > a > div > div > span > span',
            timeout=5,
        )

        posts = []
        post_containers = page.eles(
            'css:div[aria-label="Timeline: Your Home Timeline"] > div > div',
        )

        if len(post_containers) == 0:
            log_message("Couldn't found any post in the page", "WARNING")
            return []

        for container in post_containers:
            try:
                username_elem = container.ele(
                    'css:div[data-testid="User-Name"] > div:nth-child(1) > div > a > div > div > span > span',
                    timeout=0.1,
                )
                tweet_elem = container.ele(
                    'css:div[data-testid="tweetText"]', timeout=0.1
                )

                analytics_elem = container.ele(
                    'css:[aria-label*="View post analytics"]', timeout=0.1
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


async def scroll_to_find_last_post(processed_data, max_scrolls=100):
    last_post_data = processed_data["last_post"]
    if not last_post_data:
        return True

    scroll_count = 0

    # NOTE: Each scroll would find 8 to 20 posts, so max of 1000 posts would be sufficient
    while scroll_count < max_scrolls:
        start = time.time()
        posts = extract_posts()
        if scroll_count > 0:
            log_message(
                f"Found {len(posts)} posts after scrolling. total time took: {(time.time() - start):.2f}"
            )

        if len(posts) > 0:
            await store_new_posts(posts, processed_data)

        for post in posts:
            if (
                post["username"] == last_post_data["username"]
                and post["content"] == last_post_data["content"]
            ):
                log_message("Found last saved post", "INFO")
                return True

        page.scroll.down()
        scroll_count += 1

    log_message("Could not find last saved post after scrolling", "WARNING")
    await send_alert(f"<b>Couldn't Find the last saved post</b>")
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

    refresh_count = 0
    consecutive_errors = 0
    max_consecutive_errors = 5

    while True:
        await sleep_until_market_open()
        await scroll_to_find_last_post(processed_data)

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
                refresh_delay = random.uniform(60, 240)
                await asyncio.sleep(refresh_delay)

            except Exception as e:
                log_message(f"Error in main loop: {e}", "ERROR")
                consecutive_errors += 1
                await asyncio.sleep(30)  # Wait longer on errors


async def main_async():
    global tcp_client

    # Initialize and start TCP client in a separate thread
    tcp_client = EncryptedTcpClient(
        server_ip=TCP_HOST,
        server_port=TCP_PORT,
        shared_secret=TCP_SECRET,
        username=TCP_USERNAME,
    )
    tcp_client.start()  # Start in a separate thread
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
