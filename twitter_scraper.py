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
from Crypto.Util.Padding import pad, unpad
from dotenv import load_dotenv
from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage.common import Keys

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.ticker_deck_sender import initialize_ticker_deck, send_ticker_deck_message
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
TWITTER_DATA_DIR = "data/twitter_data"
LAST_POST_FILE = "data/twitter_last_post.json"
SESSION_FILE = "data/twitter_session.json"
TELEGRAM_BOT_TOKEN = os.getenv("TWITTER_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("TWITTER_TELEGRAM_GRP")
DECK_TWEET_TELEGRAM_GRP = os.getenv("DECK_TWEET_TELEGRAM_GRP")
TWITTER_USERNAME = os.getenv("TWITTER_USERNAME")
TWITTER_PASSWORD = os.getenv("TWITTER_PASSWORD")

os.makedirs("data", exist_ok=True)
os.makedirs(TWITTER_DATA_DIR, exist_ok=True)

tcp_client = None
co = ChromiumOptions()
page = ChromiumPage(co)
last_received_content = ""
last_received_cleaned_content = ""
posts_memory_cache = []  # In-memory cache for yesterday and today's posts

connected_websockets = set()

deck_senders = ["Citron Research", "6k_Investor", "TheUndefinedMystic", "GB"]


class EncryptedTcpClient:
    def __init__(self, server_ip, server_port, shared_secret, username):
        self.server_ip = server_ip
        self.server_port = server_port
        self.shared_secret = shared_secret
        self.username = username
        self.sock = None
        self.key = None
        self.connected = False
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.stop_event = threading.Event()

    def _get_utc_date(self):
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    def _derive_key(self):
        combined = self.shared_secret + self._get_utc_date()
        return hashlib.sha256(combined.encode("utf-8")).digest()

    def _encrypt(self, plaintext: str) -> bytes:
        iv = b"\x00" * 16
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        return cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))

    def _decrypt(self, ciphertext: bytes) -> str:
        iv = b"\x00" * 16
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        decrypted_padded = cipher.decrypt(ciphertext)
        return unpad(decrypted_padded, AES.block_size).decode("utf-8")

    def connect(self):
        threading.Thread(target=self._connection_loop, daemon=True).start()

    def _connection_loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                while self.connected and not self.stop_event.is_set():
                    self.cond.wait()

            if self.stop_event.is_set():
                break

            log_message("Attempting to connect...", "INFO")
            try:
                self.sock = socket.create_connection(
                    (self.server_ip, self.server_port), timeout=60
                )
                self.sock.settimeout(140)
                self.key = self._derive_key()

                # Authenticate
                iv = b"\x00" * 16
                encrypted_username = self._encrypt(self.username)
                self.sock.sendall(iv + encrypted_username)
                log_message(
                    f"[TCP] Sent encrypted auth for username '{self.username}'", "INFO"
                )

                with self.lock:
                    self.connected = True

                log_message(
                    f"[TCP] Connected to {self.server_ip}:{self.server_port}", "INFO"
                )

                threading.Thread(target=self._receive_loop, daemon=True).start()
                threading.Thread(target=self._heartbeat_loop, daemon=True).start()

                time.sleep(0.5)
                self.send_message("Hello, server!")

            except Exception as e:
                log_message(f"[TCP] Connection error: {e}", "ERROR")
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                time.sleep(2)

    def disconnect(self):
        self.stop_event.set()
        with self.lock:
            self.connected = False
            self.cond.notify()
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
        log_message("[TCP] Server disconnected", "WARNING")

    def send_message(self, message: str):
        with self.lock:
            if not self.connected:
                log_message("Cannot send message: not connected", "WARNING")
                return

        try:
            framed = (message + "<END>").encode("utf-8")
            self.sock.sendall(framed)
            if message != "HEARTBEAT":
                log_message(f"Sent: {message}", "INFO")
        except Exception as e:
            log_message(f"Send error: {e}", "ERROR")
            with self.lock:
                self.connected = False
                self.cond.notify()

    def _receive_loop(self):
        buffer = b""
        while not self.stop_event.is_set():
            try:
                data = self.sock.recv(4096)
                if not data:
                    log_message("Server closed connection", "WARNING")
                    break

                buffer += data
                while b"<END>" in buffer:
                    msg, buffer = buffer.split(b"<END>", 1)
                    try:
                        decrypted = self._decrypt(msg)
                        log_message(f"Received (decrypted): {decrypted}", "INFO")
                    except Exception:
                        text = msg.decode("utf-8", errors="ignore")
                        log_message(f"Received (plaintext): {text}", "INFO")

            except Exception as e:
                log_message(f"Receive error: {e}", "ERROR")
                break

        with self.lock:
            self.connected = False
            self.cond.notify()

    def _heartbeat_loop(self):
        while not self.stop_event.is_set():
            time.sleep(30)
            self.send_message("HEARTBEAT")


def get_date_file_path(date_obj):
    """Get the file path for storing posts of a specific date"""
    year = date_obj.strftime("%Y")
    month = date_obj.strftime("%m")
    day = date_obj.strftime("%d")

    year_dir = os.path.join(TWITTER_DATA_DIR, year)
    month_dir = os.path.join(year_dir, month)

    os.makedirs(month_dir, exist_ok=True)

    return os.path.join(month_dir, f"{day}.json")


def load_posts_for_date(date_obj):
    """Load posts for a specific date"""
    file_path = get_date_file_path(date_obj)
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_posts_for_date(date_obj, posts):
    """Save posts for a specific date"""
    file_path = get_date_file_path(date_obj)
    with open(file_path, "w") as f:
        json.dump(posts, f, indent=2)


def load_last_post():
    """Load the last processed post"""
    try:
        with open(LAST_POST_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_last_post(post_data):
    """Save the last processed post"""
    with open(LAST_POST_FILE, "w") as f:
        json.dump(post_data, f, indent=2)


def load_recent_posts_to_memory():
    """Load yesterday's and today's posts into memory cache"""
    global posts_memory_cache

    today = datetime.datetime.now().date()
    yesterday = today - datetime.timedelta(days=1)

    posts_memory_cache = []

    yesterday_posts = load_posts_for_date(yesterday)
    posts_memory_cache.extend(yesterday_posts)

    today_posts = load_posts_for_date(today)
    posts_memory_cache.extend(today_posts)

    log_message(
        f"Loaded {len(posts_memory_cache)} posts into memory cache (yesterday: {len(yesterday_posts)}, today: {len(today_posts)})",
        "INFO",
    )


async def send_alert(msg: str):
    alert = f"🚨 ALERT: {msg}\n\nPlease check the server!"
    await send_telegram_message(alert, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


def text_similarity(text1, text2, threshold=0.95):
    clean_text1 = text1.lower().strip()
    clean_text2 = text2.lower().strip()

    min_length = min(len(clean_text1), len(clean_text2))

    truncated_text1 = clean_text1[:min_length]
    truncated_text2 = clean_text2[:min_length]

    similarity = difflib.SequenceMatcher(None, truncated_text1, truncated_text2).ratio()
    return similarity >= threshold


def find_matching_post(search_content, posts_list):
    """Find matching post with timing"""
    start_time = time.time()

    for post in posts_list:
        if text_similarity(search_content, post["content"]):
            search_time = (time.time() - start_time) * 1000
            log_message(f"Post matching algorithm took: {search_time:.2f}ms", "INFO")
            return post

    search_time = (time.time() - start_time) * 1000
    log_message(
        f"Post matching algorithm took: {search_time:.2f}ms (no match found)", "INFO"
    )
    return None


async def send_deck_tweet(sender, content, timestamp):
    message = f"<b>New Deck tweet found</b>\n\n"
    message += f"<b>Sender:</b> {sender}\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>Content:</b> {content}{'\n\ncontent is trimmed.....' if len(content) > 600 else ''}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, DECK_TWEET_TELEGRAM_GRP)


async def send_found_post(data, source):
    global tcp_client

    if tcp_client and tcp_client.connected:
        tcp_client.send_message(json.dumps(data))
    else:
        await send_alert("<b>TCP_CLIENT isn't Connected</b>")

    message = f"<b>New Post sender found - {source}</b>\n\n"
    message += f"<b>Sender:</b> {data['t']}\n"
    message += f"<b>Content:</b> {data['te'][:600]}{'\n\ncontent is trimmed.....' if len(data['te']) > 600 else ''}"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)


async def websocket_ping_loop():
    """Send ping frames to all connected WebSocket clients every 10 seconds"""
    while True:
        await asyncio.sleep(10)  # Wait 10 seconds

        if connected_websockets:
            log_message(
                f"[WebSocket] Sending ping to {len(connected_websockets)} connected clients",
                "INFO",
            )

            clients_to_ping = connected_websockets.copy()

            for websocket in clients_to_ping:
                try:
                    await websocket.send(json.dumps({"dt": "ping"}))
                except websockets.exceptions.ConnectionClosed:
                    log_message(
                        "[WebSocket] Client disconnected during ping", "WARNING"
                    )
                    connected_websockets.discard(websocket)
                except Exception as e:
                    log_message(
                        f"[WebSocket] Error sending ping to client: {e}", "WARNING"
                    )
                    connected_websockets.discard(websocket)


async def handle_websocket_message(websocket):
    global posts_memory_cache
    global last_received_content
    global last_received_cleaned_content

    connected_websockets.add(websocket)
    client_address = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
    log_message(f"[WebSocket] New client connected: {client_address}", "INFO")
    log_message(
        f"[WebSocket] Total connected clients: {len(connected_websockets)}", "INFO"
    )

    try:
        async for message in websocket:
            try:
                request_start_time = time.time()
                data = json.loads(message)
                search_content = data.get("te", "")
                sender = data.get("t", "")
                dtype = data.get("dt", "")

                if dtype and dtype == "pong":
                    continue

                if not search_content:
                    await send_alert(
                        f"<b>Couldn't found post</b>\n\n<b>Reason:</b> Search content was not provided"
                    )
                    log_message(f"Search content was empty content: {data}")
                    continue

                if sender in deck_senders:
                    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
                    cleaned_content = search_content.replace(
                        "_FROM_PIXEL_", ""
                    ).replace("_FROM_SAMSUNG_", "")

                    if cleaned_content == last_received_cleaned_content:
                        log_message(
                            "Received the same content to send to deck so ignoring...",
                            "WARNING",
                        )
                    else:
                        await send_ticker_deck_message(
                            sender="twitter",
                            name=sender,
                            content=cleaned_content,
                        )

                        await send_deck_tweet(sender, search_content, timestamp)

                        last_received_cleaned_content = cleaned_content
                        log_message(
                            f"Sent new tweet to deck. tweet: {cleaned_content[:300]}...",
                            "INFO",
                        )
                    continue

                if search_content == last_received_content:
                    log_message(
                        "Received the same content to check ignoring...", "WARNING"
                    )
                    continue

                last_received_content = search_content
                log_message(
                    f"Received a search request for content: {search_content[:300]}....",
                    "INFO",
                )

                matching_post = find_matching_post(search_content, posts_memory_cache)

                if matching_post:
                    # FIXME: Send to tcp and also after that send to telegram as well and when error shows up like not found or something like that send to telegram twitter channel as well
                    response_data = {
                        "pn": "x.com",
                        "t": matching_post["username"],
                        "te": matching_post["content"],
                        "ts": time.time() * 1000,
                    }
                    total_time = (time.time() - request_start_time) * 1000
                    log_message(
                        f"Found post in memory cache. Total request time: {total_time:.2f}ms",
                        "INFO",
                    )
                    await send_found_post(response_data, "memory_cache")
                    continue

                # If not found in cache, refresh and check again
                log_message("Post not found in memory cache, refreshing feed", "INFO")
                fresh_posts_start = time.time()
                fresh_posts = await refresh_and_get_posts()
                fresh_posts_time = (time.time() - fresh_posts_start) * 1000

                if fresh_posts:
                    matching_post = find_matching_post(search_content, fresh_posts)

                    if matching_post:
                        response_data = {
                            "pn": "x.com",
                            "t": matching_post["username"],
                            "te": matching_post["content"],
                            "ts": time.time() * 1000,
                        }
                        total_time = (time.time() - request_start_time) * 1000
                        log_message(
                            f"Found post in fresh fetch. Fresh fetch took: {fresh_posts_time:.2f}ms, Total request time: {total_time:.2f}ms",
                            "INFO",
                        )
                        await send_found_post(response_data, "fresh_fetch")
                        continue

                    await store_new_posts(fresh_posts)
                    load_recent_posts_to_memory()

                total_time = (time.time() - request_start_time) * 1000
                log_message(
                    f"Post not found anywhere. Fresh fetch took: {fresh_posts_time:.2f}ms, Total request time: {total_time:.2f}ms",
                    "WARNING",
                )
                await send_alert(
                    f"<b>Couldn't found post</b>\n\n<b>Reason:</b> Couldn't find the post in memory cache/Fresh fetch\n<b>Content</b>: {search_content[:200]}\n\n content is trimmed....."
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
        log_message(f"[WebSocket] Client {client_address} disconnected", "INFO")
    except Exception as e:
        log_message(f"[WebSocket] Error handling client {client_address}: {e}", "ERROR")
    finally:
        connected_websockets.discard(websocket)
        log_message(
            f"[WebSocket] Client {client_address} removed from connected set", "INFO"
        )
        log_message(
            f"[WebSocket] Total connected clients: {len(connected_websockets)}", "INFO"
        )


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
        extraction_start = time.time()

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

        parsing_start = time.time()

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
                log_message(f"Error parsing individual post: {e}", "ERROR")
                continue

        parsing_time = (time.time() - parsing_start) * 1000
        total_extraction_time = (time.time() - extraction_start) * 1000

        log_message(
            f"Post extraction completed: {len(posts)} posts found. Parsing took: {parsing_time:.2f}ms, Total extraction: {total_extraction_time:.2f}ms",
            "INFO",
        )

        return posts

    except Exception as e:
        extraction_time = (
            (time.time() - extraction_start) * 1000
            if "extraction_start" in locals()
            else 0
        )
        log_message(
            f"Error extracting posts (took {extraction_time:.2f}ms): {e}", "ERROR"
        )
        return []


async def navigate_to_following():
    try:
        page.get(TWITTER_HOME_URL)
        await asyncio.sleep(3)

        # FIXME: fix this shit too it should propery log things
        page.ele("Following").click()
        log_message("Navigated to following page", "INFO")
        return True

    except Exception as e:
        log_message(f"Error navigating to following: {e}", "ERROR")
        return False


async def scroll_to_find_last_post(max_scrolls=100):
    last_post_data = load_last_post()
    if not last_post_data:
        return True

    scroll_count = 0

    # NOTE: Each scroll would find 8 to 20 posts, so max of 1000 posts would be sufficient
    while scroll_count < max_scrolls:
        start = time.time()
        posts = extract_posts()
        if scroll_count > 0:
            log_message(
                f"Found {len(posts)} posts after scrolling. total time took: {(time.time() - start):.2f}s"
            )

        if len(posts) > 0:
            await store_new_posts(posts)

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
        refresh_start = time.time()
        log_message(f"Starting post refresh...")

        page.scroll.to_top()
        scroll_time = time.time()

        page.refresh()
        refresh_time = time.time()

        posts = extract_posts()
        extraction_time = time.time()

        total_time = (extraction_time - refresh_start) * 1000
        scroll_duration = (scroll_time - refresh_start) * 1000
        refresh_duration = (refresh_time - scroll_time) * 1000
        extraction_duration = (extraction_time - refresh_time) * 1000

        log_message(
            f"Post refresh completed: {len(posts)} posts. Scroll: {scroll_duration:.2f}ms, Refresh: {refresh_duration:.2f}ms, Extraction: {extraction_duration:.2f}ms, Total: {total_time:.2f}ms",
            "INFO",
        )
        return posts

    except Exception as e:
        total_time = (
            (time.time() - refresh_start) * 1000 if "refresh_start" in locals() else 0
        )
        log_message(
            f"Error refreshing and getting posts (took {total_time:.2f}ms): {e}",
            "ERROR",
        )
        return []


async def store_new_posts(new_posts):
    """Store new posts in date-based files and update memory cache"""
    if not new_posts:
        return 0

    stored_count = 0
    posts_by_date = {}

    for post in new_posts:
        try:
            post_date = datetime.datetime.strptime(
                post["timestamp"], "%Y-%m-%d %H:%M:%S"
            ).date()
            if post_date not in posts_by_date:
                posts_by_date[post_date] = []
            posts_by_date[post_date].append(post)
        except ValueError:
            log_message(
                f"Invalid timestamp format in post: {post['timestamp']}", "ERROR"
            )
            continue

    for date_obj, date_posts in posts_by_date.items():
        existing_posts = load_posts_for_date(date_obj)
        existing_post_ids = {post["post_id"] for post in existing_posts}

        new_posts_for_date = []
        for post in date_posts:
            if post["post_id"] not in existing_post_ids:
                new_posts_for_date.append(post)
                stored_count += 1

        if new_posts_for_date:
            all_posts_for_date = existing_posts + new_posts_for_date
            save_posts_for_date(date_obj, all_posts_for_date)

    if stored_count > 0:
        save_last_post(new_posts[0])
        load_recent_posts_to_memory()

        log_message(
            f"Stored {stored_count} new posts across {len(posts_by_date)} dates", "INFO"
        )

    return stored_count


async def start_websocket_server():
    log_message(f"Starting WebSocket server on port {WEBSOCKET_PORT}", "INFO")
    return await websockets.serve(handle_websocket_message, "0.0.0.0", WEBSOCKET_PORT)


async def run_scraper():
    failed_login_attempts = 0
    max_failed_attempts = 3

    load_recent_posts_to_memory()

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
        await initialize_ticker_deck("Twitter")
        await scroll_to_find_last_post()

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
                    stored_count = await store_new_posts(posts)
                    if stored_count > 0:
                        log_message(
                            f"Total posts in memory cache: {len(posts_memory_cache)}",
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
                refresh_delay = random.uniform(30, 120)
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
    tcp_client.connect()
    websocket_server = await start_websocket_server()

    try:
        ping_task = asyncio.create_task(websocket_ping_loop())

        await asyncio.gather(run_scraper(), websocket_server.wait_closed(), ping_task)
    except Exception as e:
        log_message(f"Error in main async: {e}", "CRITICAL")
        websocket_server.close()
        await websocket_server.wait_closed()
        page.quit()


def main():
    if not all(
        [
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_GRP,
            TWITTER_USERNAME,
            TWITTER_PASSWORD,
            DECK_TWEET_TELEGRAM_GRP,
        ]
    ):
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
