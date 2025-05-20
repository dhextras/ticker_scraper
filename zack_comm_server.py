import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import websockets
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

# Constants
TELEGRAM_BOT_TOKEN = os.getenv("ZACKS_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("ZACKS_TELEGRAM_GRP")
STARTING_CID = 44640  # Starting comment ID
WEBSOCKET_PORT = 6969  # IDK lol just for fun
ACCOUNT_COOL_DOWN_DEFAULT = 15 * 60  # Default cool down period in seconds (15 minutes)

DATA_DIR = Path("data")
CRED_DIR = Path("cred")
COMMENT_ID_FILE = DATA_DIR / "zacks_last_comment_id.json"
CREDENTIALS_FILE = CRED_DIR / "zacks_credentials.json"
ACCOUNT_STATUS_FILE = DATA_DIR / "zacks_account_status.json"

# Global state
connected_clients = {}  # client_id -> websocket
client_status = (
    {}
)  # client_id -> {status, last_active, current_cid, account_index, account_email, account_assigned_time}
current_comment_id = STARTING_CID
accounts = []
total_accounts = 0
account_status = {}  # email -> {banned, banned_until, ban_count}
processing_queue = asyncio.Queue()  # Queue for comment IDs to process
account_locks = {}  # email -> client_id that's using this account
client_browser_restart = {}  # client_id -> next_restart_time
account_usage_queue = []  # Queue of available account emails
account_in_use = set()  # Set of emails currently in use
initializing_clients = set()  # Set of clients currently initializing
account_usage_count = {}  # email -> count of


def load_credentials():
    """Load credentials from the JSON file"""
    global accounts, total_accounts

    try:
        if CREDENTIALS_FILE.exists():
            with open(CREDENTIALS_FILE, "r") as f:
                accounts = json.load(f)
                total_accounts = len(accounts)
                if total_accounts == 0:
                    log_message("No accounts found in credentials file", "CRITICAL")
                    sys.exit(1)
                log_message(
                    f"Loaded {total_accounts} accounts from credentials file", "INFO"
                )
                return True
        else:
            log_message(f"Credentials file not found at {CREDENTIALS_FILE}", "CRITICAL")
            sys.exit(1)
    except Exception as e:
        log_message(f"Error loading credentials: {e}", "CRITICAL")
        sys.exit(1)


def load_account_status():
    """Load account status from file"""
    global account_status

    # NOTE: If you add more or remove accounts remove this account status file don't wanna fix this shitty issue for now
    try:
        if ACCOUNT_STATUS_FILE.exists():
            with open(ACCOUNT_STATUS_FILE, "r") as f:
                account_status = json.load(f)

                current_time = datetime.now().timestamp()
                for email in list(account_status.keys()):
                    if account_status[email]["banned_until"] <= current_time:
                        account_status[email]["banned"] = False
                        account_status[email]["banned_until"] = 0

                banned_accounts = [
                    email
                    for email, status in account_status.items()
                    if status["banned"]
                ]
                if banned_accounts:
                    log_message(
                        f"Currently banned accounts: {', '.join(banned_accounts)}",
                        "INFO",
                    )

                return True
        else:
            for account in accounts:
                account_status[account["email"]] = {
                    "banned": False,
                    "banned_until": 0,
                    "ban_count": 0,
                }
            save_account_status()
    except Exception as e:
        log_message(f"Error loading account status: {e}", "ERROR")
        for account in accounts:
            account_status[account["email"]] = {
                "banned": False,
                "banned_until": 0,
                "ban_count": 0,
            }
        save_account_status()

    return True


def save_account_status():
    """Save account status to file"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(ACCOUNT_STATUS_FILE, "w") as f:
            json.dump(account_status, f)
        return True
    except Exception as e:
        log_message(f"Error saving account status: {e}", "ERROR")
        return False


def init_account_rotation():
    """Initialize the account rotation system"""
    global account_usage_queue, account_in_use, account_usage_count

    # Start with all accounts in the queue
    account_usage_queue = [account["email"] for account in accounts]
    # Shuffle the queue for fairness
    random.shuffle(account_usage_queue)
    account_in_use = set()

    # Initialize usage counter for each account
    account_usage_count = {account["email"]: 0 for account in accounts}

    log_message(
        f"Initialized account rotation with {len(account_usage_queue)} accounts", "INFO"
    )


def get_available_account(client_id):
    """Get an available account using a rotation system with 30-minute persistence per client"""
    global account_usage_queue, account_in_use, client_status, account_usage_count

    current_time = datetime.now().timestamp()

    # Check if client already has an assigned account and it hasn't been 30 minutes yet
    if client_id in client_status and "account_email" in client_status[client_id]:
        assigned_email = client_status[client_id].get("account_email")
        assignment_time = client_status[client_id].get("account_assigned_time", 0)

        # If the client has an account assigned less than 30 minutes ago, reuse it
        if (
            assigned_email and current_time - assignment_time < 1800
        ):  # 30 minutes = 1800 seconds
            # Make sure the account isn't banned
            is_banned = (
                assigned_email in account_status
                and account_status[assigned_email]["banned"]
                and current_time < account_status[assigned_email]["banned_until"]
            )

            if not is_banned:
                # Find the account index
                account_idx = None
                for idx, account in enumerate(accounts):
                    if account["email"] == assigned_email:
                        account_idx = idx
                        break

                if account_idx is not None:
                    log_message(
                        f"Reusing assigned account {assigned_email} for client {client_id} (assigned {(current_time - assignment_time)/60:.1f} minutes ago)",
                        "INFO",
                    )
                    return account_idx, assigned_email, False
            else:
                # Account is banned, clear the assignment
                log_message(
                    f"Previously assigned account {assigned_email} is now banned, assigning new account to client {client_id}",
                    "WARNING",
                )
                client_status[client_id].pop("account_email", None)
                client_status[client_id].pop("account_assigned_time", None)

    # If we need a new account, continue with the rotation system

    # If queue is empty but we have accounts in use, wait for releases
    if not account_usage_queue and len(account_in_use) >= total_accounts:
        log_message(
            f"All accounts are in use, client {client_id} will have to wait", "WARNING"
        )
        return None, None, None

    # Find the least used accounts that are available
    available_accounts = []
    for email in account_usage_queue:
        # Check if account is banned
        if email not in account_status:
            account_status[email] = {
                "banned": False,
                "banned_until": 0,
                "ban_count": 0,
            }

        is_banned = (
            account_status[email]["banned"]
            and current_time < account_status[email]["banned_until"]
        )

        if not is_banned:
            available_accounts.append((email, account_usage_count.get(email, 0)))

    # If we have available accounts, select the least used one
    if available_accounts:
        # Sort by usage count (ascending)
        available_accounts.sort(key=lambda x: x[1])

        # Get the email with the lowest usage count
        email, _ = available_accounts[0]

        # Remove this email from the queue
        account_usage_queue.remove(email)

        # Find the account details
        account_idx = None
        for idx, account in enumerate(accounts):
            if account["email"] == email:
                account_idx = idx
                break

        if account_idx is None:
            log_message(f"Account {email} not found in accounts list", "ERROR")
            return None, None, None

        # Increment usage count
        account_usage_count[email] = account_usage_count.get(email, 0) + 1

        # Add to in-use set
        account_in_use.add(email)

        # Store the assignment info in client_status for future reference
        client_status[client_id]["account_email"] = email
        client_status[client_id]["account_assigned_time"] = current_time

        log_message(
            f"Assigned account {email} to client {client_id} (usage count: {account_usage_count[email]})",
            "INFO",
        )
        return account_idx, email, False

    # If no non-banned accounts in queue, try to find the account with earliest ban expiration
    earliest_expiry = float("inf")
    earliest_email = None
    earliest_idx = None

    for idx, account in enumerate(accounts):
        email = account["email"]

        if email in account_in_use:
            continue

        if email not in account_status:
            account_status[email] = {
                "banned": False,
                "banned_until": 0,
                "ban_count": 0,
            }

        ban_until = account_status[email]["banned_until"]

        if ban_until < earliest_expiry:
            earliest_expiry = ban_until
            earliest_email = email
            earliest_idx = idx

    if earliest_email:
        wait_time = max(0, earliest_expiry - current_time)

        # Increment usage count
        account_usage_count[earliest_email] = (
            account_usage_count.get(earliest_email, 0) + 1
        )

        # Add to in-use set
        account_in_use.add(earliest_email)

        # Store the assignment info in client_status for future reference
        client_status[client_id]["account_email"] = earliest_email
        client_status[client_id]["account_assigned_time"] = current_time

        log_message(
            f"All non-banned accounts are in use or none available. Using {earliest_email} for client {client_id}, available in {wait_time:.1f} seconds (usage count: {account_usage_count[earliest_email]})",
            "WARNING",
        )
        return earliest_idx, earliest_email, True

    log_message(f"No accounts available for client {client_id}", "ERROR")
    return None, None, None


def release_account(email):
    """Release an account back to the rotation queue"""
    global account_usage_queue, account_in_use

    if email in account_in_use:
        account_in_use.remove(email)

        current_time = datetime.now().timestamp()
        is_banned = account_status.get(email, {}).get(
            "banned", False
        ) and current_time < account_status.get(email, {}).get("banned_until", 0)

        if not is_banned:
            # Add to the end of the queue
            account_usage_queue.append(email)
            log_message(f"Released account {email} back to rotation queue", "INFO")
            return True
        else:
            log_message(
                f"Released banned account {email} (not added back to queue until ban expires)",
                "INFO",
            )
            return True
    return False


def release_client_account(client_id):
    """Release the account assigned to a client"""
    if client_id in client_status and "account_email" in client_status[client_id]:
        email = client_status[client_id]["account_email"]
        result = release_account(email)

        # Clear the assignment
        client_status[client_id].pop("account_email", None)
        client_status[client_id].pop("account_assigned_time", None)

        return result
    return False


def ban_account(email, minutes=None):
    """Mark an account as banned and set the cool-down period"""
    global account_status, account_usage_queue, account_in_use

    if email not in account_status:
        account_status[email] = {"banned": False, "banned_until": 0, "ban_count": 0}

    cool_down_seconds = ACCOUNT_COOL_DOWN_DEFAULT
    if minutes:
        cool_down_seconds = minutes * 60

    current_time = datetime.now().timestamp()
    banned_until = current_time + cool_down_seconds

    account_status[email]["banned"] = True
    account_status[email]["banned_until"] = banned_until
    account_status[email]["ban_count"] += 1

    # When banning an account, remove from in-use set
    if email in account_in_use:
        account_in_use.remove(email)

    # Remove from rotation queue if present
    if email in account_usage_queue:
        account_usage_queue.remove(email)

    ban_expiry_time = datetime.fromtimestamp(banned_until).strftime("%Y-%m-%d %H:%M:%S")
    log_message(
        f"Account {email} banned until {ban_expiry_time} ({cool_down_seconds/60:.1f} minutes)",
        "ERROR",
    )

    save_account_status()
    return True


def load_last_comment_id():
    """Load the last processed comment ID from file"""
    try:
        if COMMENT_ID_FILE.exists():
            with open(COMMENT_ID_FILE, "r") as f:
                data = json.load(f)
                return data.get("last_comment_id", STARTING_CID)
        return STARTING_CID
    except Exception as e:
        log_message(f"Error loading last comment ID: {e}", "ERROR")
        return STARTING_CID


async def save_comment_id(comment_id: int):
    """Save the last processed comment ID"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(COMMENT_ID_FILE, "w") as f:
            json.dump({"last_comment_id": comment_id}, f)
    except Exception as e:
        log_message(f"Error saving comment ID: {e}", "ERROR")


def extract_ticker(title, content):
    if title == "We're Buying and Selling Today":
        buy_section = re.search(r"(Buy .*? Today)", content)
        if buy_section:
            match = re.search(r"\(([A-Z]+)\)", content[buy_section.start() :])
            if match:
                return match.group(1), "Buy"
    elif "BUY" in title or "Buy" in title or "Buying" in title:
        if "sell" in title.lower():
            match = re.search("buy", content.lower())
            match2 = re.search("hold", content.lower())
            if match:
                content = content[match.end() :]
            elif match2:
                content = content[match2.end() :]
        match = re.search(r"\(([A-Z]+)\)", content)
        if match:
            return match.group(1), "Buy"
    elif "Adding" in title:
        match = re.search(r"Adding\s+([A-Z]+)", title)
        if match:
            return match.group(1), "Buy"
    # TODO: Later also process sell alerts

    return None, None


async def handle_client_pong(websocket, client_id):
    """Handle ping-pong mechanism for a client"""
    try:
        while client_id in connected_clients:
            await websocket.send(json.dumps({"type": "ping"}))
            client_status[client_id]["last_active"] = time.time()
            await asyncio.sleep(5)  # Send ping every 5 seconds
    except Exception as e:
        log_message(f"Error in ping-pong handler for client {client_id}: {e}", "ERROR")


async def browser_initialization_manager():
    """Manage browser initialization for all connected clients"""
    global initializing_clients
    try:
        while True:
            current_time = time.time()

            # Check all connected clients
            for client_id in list(connected_clients.keys()):
                if client_id not in client_browser_restart:
                    # New client needs initial setup
                    client_browser_restart[client_id] = current_time
                    initializing_clients.add(client_id)

                    # Get an account for initialization
                    account_idx, email, _ = get_available_account(client_id)

                    if account_idx is not None and client_id in connected_clients:
                        try:
                            await connected_clients[client_id].send(
                                json.dumps(
                                    {
                                        "type": "initialize_login",
                                        "account_index": account_idx,
                                        "email": email,
                                    }
                                )
                            )
                            log_message(
                                f"Sent initial login request to client {client_id} with account {email}",
                                "INFO",
                            )
                        except Exception as e:
                            log_message(
                                f"Error sending initialization to client {client_id}: {e}",
                                "ERROR",
                            )
                            if email:
                                release_account(email)
                            if client_id in initializing_clients:
                                initializing_clients.remove(client_id)

                # Check if it's time for a browser restart (every 30 minutes)
                elif (
                    client_id in client_browser_restart
                    and current_time - client_browser_restart[client_id] >= 1800
                ):
                    if (
                        client_id not in initializing_clients
                        and client_id in connected_clients
                    ):
                        initializing_clients.add(client_id)

                        release_client_account(client_id)
                        account_idx, email, _ = get_available_account(client_id)

                        if account_idx is not None:
                            try:
                                await connected_clients[client_id].send(
                                    json.dumps(
                                        {
                                            "type": "restart_browser",
                                            "account_index": account_idx,
                                            "email": email,
                                        }
                                    )
                                )
                                log_message(
                                    f"Sent browser restart request to client {client_id}",
                                    "INFO",
                                )
                            except Exception as e:
                                log_message(
                                    f"Error sending restart to client {client_id}: {e}",
                                    "ERROR",
                                )
                                if email:
                                    release_account(email)
                                if client_id in initializing_clients:
                                    initializing_clients.remove(client_id)
                elif client_id in initializing_clients:
                    initializing_clients.remove(client_id)

            await asyncio.sleep(10)  # Check every 10 seconds
    except Exception as e:
        log_message(f"Error in browser initialization manager: {e}", "ERROR")
        await asyncio.sleep(30)
        asyncio.create_task(browser_initialization_manager())


async def handle_client(websocket):
    """Handle WebSocket client connections"""
    global current_comment_id, initializing_clients
    client_id = None

    try:
        # Wait for client registration
        message = await websocket.recv()
        data = json.loads(message)

        if data["type"] == "register":
            client_id = data["client_id"]
            connected_clients[client_id] = websocket
            client_status[client_id] = {
                "status": "available",
                "last_active": time.time(),
                "current_cid": None,
                "account_index": None,
                "processing_time": [],
            }
            log_message(f"Client {client_id} connected", "INFO")
            initializing_clients.add(client_id)

            # Send acknowledgment
            await websocket.send(
                json.dumps({"type": "registration_ack", "client_id": client_id})
            )

            # Start ping-pong handler for this client
            ping_pong_task = asyncio.create_task(
                handle_client_pong(websocket, client_id)
            )
            await ping_pong_task

            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)

                    if data["type"] == "status_update":
                        client_status[client_id]["status"] = data["status"]
                        client_status[client_id]["last_active"] = time.time()

                        if (
                            data["status"] == "available"
                            and client_id not in initializing_clients
                        ):
                            await processing_queue.put(client_id)

                    elif data["type"] == "result":
                        cid = data["comment_id"]
                        account_index = client_status[client_id].get("account_index")

                        # Release the account back to the rotation queue
                        if account_index is not None and account_index < len(accounts):
                            email = accounts[account_index]["email"]
                            release_account(email)

                        if "processing_start_time" in data and not data.get(
                            "browser_restart", False
                        ):
                            processing_time = (
                                time.time() - data["processing_start_time"]
                            )
                            client_status[client_id]["processing_time"].append(
                                processing_time
                            )

                            recent_times = client_status[client_id]["processing_time"][
                                -1:
                            ]
                            if (
                                len(recent_times) >= 1
                                and sum(recent_times) / len(recent_times) > 5.0
                            ):
                                # NOTE: Later change this to warning instead of error also change 1 to 5
                                log_message(
                                    f"Client {client_id} is consistently slow (avg: {sum(recent_times)/len(recent_times):.2f}s)",
                                    "WARNING",
                                )

                        if "html_content" in data and data["html_content"]:
                            await process_commentary_result(cid, data)
                        else:
                            log_message(
                                f"No content found for comment ID {cid} from client {client_id}",
                                "INFO",
                            )

                        client_status[client_id]["status"] = "available"
                        client_status[client_id]["current_cid"] = None
                        client_status[client_id]["account_index"] = None

                        if client_id not in initializing_clients:
                            await processing_queue.put(client_id)

                    elif data["type"] == "account_banned":
                        account_index = data.get("account_index")
                        if account_index is not None and account_index < len(accounts):
                            email = accounts[account_index]["email"]
                            minutes = data.get("minutes", None)
                            ban_account(email, minutes)

                        client_status[client_id]["status"] = "available"
                        client_status[client_id]["account_index"] = None

                        if client_id not in initializing_clients:
                            await processing_queue.put(client_id)

                    elif data["type"] == "pong":
                        # Update last active time on pong response
                        client_status[client_id]["last_active"] = time.time()

                    elif data["type"] == "login_result":
                        account_index = data.get("account_index")
                        success = data.get("success", False)

                        if account_index is not None and account_index < len(accounts):
                            email = accounts[account_index]["email"]

                            if success:
                                log_message(
                                    f"Client {client_id} successfully logged in with account {email}",
                                    "INFO",
                                )
                                client_status[client_id]["status"] = "available"
                                if client_id in initializing_clients:
                                    initializing_clients.remove(client_id)
                                await processing_queue.put(client_id)
                            else:
                                ban_minutes = data.get("minutes", 15)
                                log_message(
                                    f"Client {client_id} failed to login with account {email}, banned for {ban_minutes} mins",
                                    "ERROR",
                                )
                                ban_account(email, ban_minutes)

                                # Get another account for this client
                                if client_id in initializing_clients:
                                    new_account_idx, new_email, _ = (
                                        get_available_account(client_id)
                                    )
                                    if new_account_idx is not None:
                                        await websocket.send(
                                            json.dumps(
                                                {
                                                    "type": "initialize_login",
                                                    "account_index": new_account_idx,
                                                    "email": new_email,
                                                }
                                            )
                                        )
                                        log_message(
                                            f"Sent new login request to client {client_id} with account {new_email}",
                                            "INFO",
                                        )
                                    else:
                                        initializing_clients.remove(client_id)
                                        client_status[client_id]["status"] = "available"
                                        await processing_queue.put(client_id)
                        else:
                            if client_id in initializing_clients:
                                initializing_clients.remove(client_id)

                    elif data["type"] == "browser_restart_complete":
                        account_index = client_status[client_id].get("account_index")

                        if account_index is not None and account_index < len(accounts):
                            email = accounts[account_index]["email"]
                            release_account(email)

                        log_message(
                            f"Client {client_id} completed browser restart", "INFO"
                        )
                        client_browser_restart[client_id] = (
                            time.time()
                        )  # Reset restart timer
                        if client_id in initializing_clients:
                            initializing_clients.remove(client_id)
                        client_status[client_id]["status"] = "available"
                        client_status[client_id]["account_index"] = None
                        await processing_queue.put(client_id)

                except asyncio.TimeoutError:
                    pass
                except websockets.exceptions.ConnectionClosed:
                    log_message(f"Connection closed for client {client_id}", "WARNING")
                    client_status.pop(client_id)
                    del connected_clients[client_id]
                except Exception as e:
                    log_message(
                        f"Error processing message from client {client_id}: {e}",
                        "ERROR",
                    )
                finally:
                    ping_pong_task.cancel()

    except websockets.exceptions.ConnectionClosed:
        log_message(f"Connection closed for client {client_id}", "WARNING")
        client_status.pop(client_id)
        del connected_clients[client_id]
    except Exception as e:
        log_message(f"Error handling client {client_id}: {e}", "ERROR")
    finally:
        if client_id and client_id in connected_clients:
            if client_id in client_status:
                account_index = client_status[client_id].get("account_index")
                if account_index is not None and account_index < len(accounts):
                    email = accounts[account_index]["email"]
                    release_account(email)
                del client_status[client_id]

            if client_id in initializing_clients:
                initializing_clients.remove(client_id)

            if client_id in client_browser_restart:
                del client_browser_restart[client_id]

            del connected_clients[client_id]


async def process_commentary_result(comment_id, data):
    """Process commentary result from client"""
    global current_comment_id
    title = data.get("title")
    content = data.get("content")

    if not title or not content:
        return

    ticker, action = extract_ticker(title, content)

    fetched_time = get_current_time()
    ticker_info = ""

    if ticker and action:
        ticker_info = f"\n<b>Action:</b> {action} {ticker}"

        try:
            await send_ws_message(
                {
                    "name": "Zacks - Commentary",
                    "type": action,
                    "ticker": ticker,
                    "sender": "zacks",
                    "target": "CSS",
                }
            )
        except Exception as e:
            log_message(f"Error sending websocket message: {e}", "WARNING")

    log_message(f"Found comment: {comment_id}, Title: {title}", "INFO")

    message = (
        f"<b>New Zacks Commentary!</b>\n"
        f"<b>Current Time:</b> {fetched_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"<b>Comment Id:</b> {comment_id}{ticker_info}\n\n"
        f"<b>Title:</b> {title}\n\n"
        f"{content[:600]}\n\n\nthere is more......."
    )

    current_comment_id += 1
    await save_comment_id(current_comment_id)

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)


async def job_distributor():
    """Distribute jobs to available clients"""
    global current_comment_id, initializing_clients
    last_assignment_time = 0
    MIN_ASSIGNMENT_INTERVAL = 0.8  # NOTE: Increase this shit too if needed

    while True:
        await sleep_until_market_open()
        await initialize_websocket()
        log_message("Market is open. Starting commentary monitoring...", "INFO")

        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()
            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open.", "INFO")
                break

            try:
                # To avoid getting stuck in the queue, use timeout
                try:
                    client_id = await asyncio.wait_for(
                        processing_queue.get(), timeout=10
                    )
                except asyncio.TimeoutError:
                    continue

                if (
                    client_id not in connected_clients
                    or client_id not in client_status
                    or client_status[client_id]["status"] != "available"
                    or client_id in initializing_clients
                ):
                    continue

                current_time = time.time()
                if current_time - last_assignment_time < MIN_ASSIGNMENT_INTERVAL:
                    await asyncio.sleep(
                        MIN_ASSIGNMENT_INTERVAL - (current_time - last_assignment_time)
                    )

                websocket = connected_clients[client_id]

                account_idx, email, is_banned = get_available_account(client_id)

                if account_idx is None:
                    log_message(
                        f"No accounts available for client {client_id}", "ERROR"
                    )
                    await asyncio.sleep(10)
                    await processing_queue.put(client_id)
                    continue

                cid_to_check = current_comment_id

                log_message(
                    f"Assigning comment ID {cid_to_check} to `{client_id}` with account `{email}`",
                    "INFO",
                )

                # Update client status
                client_status[client_id]["status"] = "busy"
                client_status[client_id]["current_cid"] = cid_to_check
                client_status[client_id]["account_index"] = account_idx

                # Update the last assignment time
                last_assignment_time = time.time()

                await websocket.send(
                    json.dumps(
                        {
                            "type": "job",
                            "comment_id": cid_to_check,
                            "account_index": account_idx,
                            "email": email,
                            "is_banned": is_banned,
                            "processing_start_time": time.time(),
                        }
                    )
                )

            except Exception as e:
                log_message(f"Error in job distributor: {e}", "ERROR")
                await asyncio.sleep(5)


async def cleanup_inactive_clients():
    """Clean up inactive client connections"""
    while True:
        try:
            current_time = time.time()
            inactive_clients = []

            for client_id, status in client_status.items():
                # If client hasn't sent a status update in 30 seconds
                if current_time - status["last_active"] > 30:
                    inactive_clients.append(client_id)

            for client_id in inactive_clients:
                if client_id in connected_clients:
                    try:
                        await connected_clients[client_id].close()
                    except:
                        pass
                    del connected_clients[client_id]

                if client_id in client_status:
                    del client_status[client_id]

                log_message(f"Removed inactive client {client_id}", "INFO")

        except Exception as e:
            log_message(f"Error in cleanup task: {e}", "WARNING")

        await asyncio.sleep(60)  # Check every minute


async def main():
    """Main server function"""
    global current_comment_id, account_usage_count

    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        DATA_DIR.mkdir(exist_ok=True)
        CRED_DIR.mkdir(exist_ok=True)

        load_credentials()
        load_account_status()
        init_account_rotation()

        current_comment_id = load_last_comment_id()

        # Start the WebSocket server
        server = await websockets.serve(handle_client, "0.0.0.0", WEBSOCKET_PORT)

        log_message(f"Server started on port {WEBSOCKET_PORT}", "INFO")

        browser_init_task = asyncio.create_task(browser_initialization_manager())
        distributor_task = asyncio.create_task(job_distributor())
        cleanup_task = asyncio.create_task(cleanup_inactive_clients())

        await asyncio.gather(
            server.wait_closed(), browser_init_task, distributor_task, cleanup_task
        )

    except KeyboardInterrupt:
        log_message("Server shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in server: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
