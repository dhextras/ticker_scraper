import asyncio
import json
import os
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
client_status = {}  # client_id -> {status, last_active, current_cid, account_index}
current_comment_id = STARTING_CID
accounts = []
total_accounts = 0
account_status = {}  # email -> {banned, banned_until, ban_count}
processing_queue = asyncio.Queue()  # Queue for comment IDs to process


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


def get_available_account(client_id):
    """Get an available account for a specific client"""
    # Calculate which accounts this client should use
    client_index = int(client_id.split("-")[-1]) if "-" in client_id else 0
    client_accounts = []

    accounts_per_client = max(1, total_accounts // max(1, len(connected_clients)))

    for i in range(accounts_per_client):
        account_index = (client_index * accounts_per_client + i) % total_accounts
        client_accounts.append(account_index)

    # Try to find a non-banned account for this client
    current_time = datetime.now().timestamp()

    for account_idx in client_accounts:
        account = accounts[account_idx]
        email = account["email"]

        if email not in account_status:
            account_status[email] = {
                "banned": False,
                "banned_until": 0,
                "ban_count": 0,
            }

        if (
            not account_status[email]["banned"]
            or current_time > account_status[email]["banned_until"]
        ):
            if account_status[email]["banned"]:
                account_status[email]["banned"] = False
                account_status[email]["banned_until"] = 0
                log_message(
                    f"Account {email} ban period has expired, now available", "INFO"
                )
                save_account_status()

            return account_idx, account["email"], account["password"], False

    # If all assigned accounts are banned, find the one with earliest expiration
    earliest_expiry = float("inf")
    earliest_idx = -1

    for account_idx in client_accounts:
        account = accounts[account_idx]
        email = account["email"]
        if account_status[email]["banned_until"] < earliest_expiry:
            earliest_expiry = account_status[email]["banned_until"]
            earliest_idx = account_idx

    if earliest_idx >= 0:
        account = accounts[earliest_idx]
        wait_time = max(
            0, account_status[account["email"]]["banned_until"] - current_time
        )
        # NOTE: Later turn this into warning
        log_message(
            f"All accounts for client {client_id} are banned. Earliest available in {wait_time:.1f} seconds",
            "ERROR",
        )
        return earliest_idx, account["email"], account["password"], True

    return None, None, None, None


def ban_account(email, minutes=None):
    """Mark an account as banned and set the cool-down period"""
    global account_status

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


async def handle_client(websocket):
    """Handle WebSocket client connections"""
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

            # Send acknowledgment
            await websocket.send(
                json.dumps({"type": "registration_ack", "client_id": client_id})
            )

            # Main client interaction loop
            while True:
                message = await websocket.recv()
                data = json.loads(message)

                if data["type"] == "status_update":
                    client_status[client_id]["status"] = data["status"]
                    client_status[client_id]["last_active"] = time.time()

                    if data["status"] == "available":
                        await processing_queue.put(client_id)

                elif data["type"] == "result":
                    cid = data["comment_id"]

                    if "start_time" in data:
                        processing_time = time.time() - data["start_time"]
                        client_status[client_id]["processing_time"].append(
                            processing_time
                        )

                        # Check if processing is consistently slow
                        recent_times = client_status[client_id]["processing_time"][-1:]
                        if (
                            len(recent_times) >= 1
                            and sum(recent_times) / len(recent_times) > 5.0
                        ):
                            # NOTE: Later change this to warning instead of error also change 1 to 5
                            log_message(
                                f"Client {client_id} is consistently slow (avg: {sum(recent_times)/len(recent_times):.2f}s)",
                                "ERROR",
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
                    await processing_queue.put(client_id)

                elif data["type"] == "account_banned":
                    account_index = data.get("account_index")
                    if account_index is not None and account_index < len(accounts):
                        email = accounts[account_index]["email"]
                        minutes = data.get("minutes", None)
                        ban_account(email, minutes)

                    client_status[client_id]["status"] = "available"
                    await processing_queue.put(client_id)

    except websockets.exceptions.ConnectionClosed:
        log_message(f"Connection closed for client {client_id}", "WARNING")
    except Exception as e:
        log_message(f"Error handling client {client_id}: {e}", "ERROR")
    finally:
        if client_id and client_id in connected_clients:
            del connected_clients[client_id]
            if client_id in client_status:
                del client_status[client_id]


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

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    if comment_id >= current_comment_id:
        current_comment_id = comment_id + 1
        await save_comment_id(current_comment_id)


async def job_distributor():
    """Distribute jobs to available clients"""
    global current_comment_id
    last_assignment_time = 0
    MIN_ASSIGNMENT_INTERVAL = 0.8  # NOTE: Increase this shit too if needed

    # Keep track of whether the current CID is being processed
    current_cid_being_processed = False

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
                client_id = await processing_queue.get()

                if (
                    client_id not in connected_clients
                    or client_id not in client_status
                    or client_status[client_id]["status"] != "available"
                ):
                    continue

                if current_cid_being_processed:
                    await asyncio.sleep(2)
                    await processing_queue.put(client_id)
                    continue

                current_time = time.time()
                if current_time - last_assignment_time < MIN_ASSIGNMENT_INTERVAL:
                    await asyncio.sleep(MIN_ASSIGNMENT_INTERVAL)

                websocket = connected_clients[client_id]

                account_idx, email, password, is_banned = get_available_account(
                    client_id
                )

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

                # Mark this CID as being processed
                current_cid_being_processed = True

                # Update the last assignment time
                last_assignment_time = time.time()

                await websocket.send(
                    json.dumps(
                        {
                            "type": "job",
                            "comment_id": cid_to_check,
                            "account_index": account_idx,
                            "email": email,
                            "password": password,
                            "is_banned": is_banned,
                        }
                    )
                )

            except Exception as e:
                log_message(f"Error in job distributor: {e}", "ERROR")
                current_cid_being_processed = False
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
    global current_comment_id

    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        DATA_DIR.mkdir(exist_ok=True)
        CRED_DIR.mkdir(exist_ok=True)

        load_credentials()
        load_account_status()

        current_comment_id = load_last_comment_id()

        # Start the WebSocket server
        server = await websockets.serve(handle_client, "0.0.0.0", WEBSOCKET_PORT)

        log_message(f"Server started on port {WEBSOCKET_PORT}", "INFO")

        # Start background tasks
        distributor_task = asyncio.create_task(job_distributor())
        cleanup_task = asyncio.create_task(cleanup_inactive_clients())

        await asyncio.gather(server.wait_closed(), distributor_task, cleanup_task)

    except KeyboardInterrupt:
        log_message("Server shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in server: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
