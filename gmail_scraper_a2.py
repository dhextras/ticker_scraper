import asyncio
import base64
import email
import json
import os
import re
import sys
from datetime import datetime

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TELEGRAM_BOT_TOKEN = os.getenv("GMAIL_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("GMAIL_SCRAPER_TELEGRAM_GRP")
PROCESSED_IDS_FILE = "data/gmail_processed_ids_file_2.json"

os.makedirs("cred", exist_ok=True)
os.makedirs("data", exist_ok=True)


def load_last_alert():
    if os.path.exists(PROCESSED_IDS_FILE):
        with open(PROCESSED_IDS_FILE, "r") as f:
            return json.load(f)
    return []


def get_gmail_service():
    creds = None
    if os.path.exists("cred/gmail_token_a2.json"):
        creds = Credentials.from_authorized_user_file(
            "cred/gmail_token_a2.json", SCOPES
        )
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "cred/gmail_credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open("cred/gmail_token_a2.json", "w") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_header(headers, name):
    for header in headers:
        if header["name"].lower() == name.lower():
            return header["value"]
    return None


def decode_base64(encoded_str):
    decoded_bytes = base64.urlsafe_b64decode(encoded_str)
    return decoded_bytes.decode("utf-8")


def get_email_body(msg):
    if "parts" in msg["payload"]:
        for part in msg["payload"]["parts"]:
            if part["mimeType"] == "text/plain":
                return decode_base64(part["body"]["data"])
            elif part["mimeType"] == "text/html":
                return decode_base64(part["body"]["data"])
    elif msg["payload"]["mimeType"] == "text/plain":
        return decode_base64(msg["payload"]["body"]["data"])
    return ""


def format_email_received_time(internal_date):
    """Convert Gmail internalDate (milliseconds) to formatted string"""
    try:
        timestamp_seconds = int(internal_date) / 1000
        received_time = datetime.fromtimestamp(timestamp_seconds)
        return received_time.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return internal_date


def analyze_email_from_oxfordclub(email_body):
    action_to_take = "Action to Take"
    action_index = email_body.find(action_to_take)
    if action_index != -1:
        after_action_text = email_body[action_index + len(action_to_take) :]
        after_action_text = re.sub(
            r"[.,/|~`'!@#$%^&*?+_=<>\-\"\[\]\{\}]", "", after_action_text
        )  # Clean out unnecessary characters
        buy_pattern = r"Buy\s+([A-Za-z\s]+)\s*\(\s*(?:NYSE|NASDAQ):\s*([A-Z]+)\s*\)"
        match = re.search(buy_pattern, after_action_text, re.IGNORECASE)
        if match:
            return match.group(2) if match.group(2) else match.group(1).strip()
    return None


def analyze_email_from_artoftrading(subject):
    if "ALERT: Long" in subject:
        stock_symbol_pattern = r"ALERT: Long\s*\$([A-Z]+)"
        match = re.search(stock_symbol_pattern, subject)
        if match:
            return match.group(1)
    return None


def analyze_email_from_banyan(subject):
    if "Trade Alert: Buy" in subject:
        parentheses_pattern = r"Trade Alert: Buy.*?\((?:NYSE:\s*)?([A-Z]+)\)"
        match = re.search(parentheses_pattern, subject)
        if match:
            return match.group(1)

        direct_pattern = r"Trade Alert: Buy\s*([A-Z]+)"
        match = re.search(direct_pattern, subject)
        if match:
            return match.group(1)

    return None


def analyze_email_from_investors(subject):
    if "watchlist" in subject.lower():
        return None
    keywords = ["joins", "increasing", "raised", "adding", "moves to", "rejoins"]
    if any(keyword in subject.lower() for keyword in keywords):
        stock_symbol_pattern = r"\b([A-Z]{2,})\b"
        match = re.search(stock_symbol_pattern, subject)
        if match:
            return match.group(1)
    return None


def analyze_email_from_fuzzypanda(email_body):
    pattern = (
        r"Fuzzy Panda Research is\s+\*?\*?Short\*?\*?\s+(?:[A-Za-z\s]+)?\(([A-Z]+)\)"
    )

    matches = re.findall(pattern, email_body)

    if matches:
        return matches[0]

    return None


async def process_email(service, message_id):
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    headers = msg["payload"]["headers"]
    from_header = get_header(headers, "From")
    subject = get_header(headers, "Subject")
    from_email = email.utils.parseaddr(from_header)[1]
    email_body = get_email_body(msg)
    received_timestamp = format_email_received_time(msg.get("internalDate", ""))

    log_message(f"Processing email from: {from_email}", "INFO")
    log_message(f"Subject: {subject}", "INFO")

    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")
    stock_symbol = None
    sender_type = None
    order_type = "Buy"  # Default order type
    target = None

    if from_email in ["oxford@mp.oxfordclub.com", "oxford@mb.oxfordclub.com"]:
        sender_type = "oxfordclub"
        stock_symbol = analyze_email_from_oxfordclub(email_body)
    elif from_email == "stewie@artoftrading.net":
        sender_type = "stewie"
        stock_symbol = analyze_email_from_artoftrading(subject)
    elif from_email == "info@mp.banyanhill.com":
        sender_type = "banyan"
        stock_symbol = analyze_email_from_banyan(subject)
    elif from_email == "info@fuzzypandaresearch.com":
        sender_type = "fuzzypanda"
        stock_symbol = analyze_email_from_fuzzypanda(email_body)
        order_type = "Sell"
        target = "CSS"
    # elif from_email == "do-not-reply@mail.investors.com":
    #     stock_symbol = analyze_email_from_investors(subject)
    else:
        message = f"<b>New Ignorable message</b>\n\n"
        message += f"<b>Current Time:</> {timestamp}\n"
        message += f"<b>Received Time:</> {received_timestamp}\n"
        message += f"<b>Sender:</> {from_email}\n"
        message += f"<b>Subject:</> {subject}\n"

        await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    if stock_symbol and sender_type:
        await send_stock_alert(
            timestamp,
            received_timestamp,
            from_email,
            sender_type,
            stock_symbol,
            order_type,
            target,
        )


async def send_stock_alert(
    timestamp,
    rc_timestamp,
    sender,
    sender_type,
    stock_symbol,
    order_type="Buy",
    target=None,
):
    message = f"<b>New Stock Alert A2</b>\n\n"
    message += f"<b>Current Time:</b> {timestamp}\n"
    message += f"<b>Received Time:</b> {rc_timestamp}\n"
    message += f"<b>Sender:</b> {sender}\n"
    message += f"<b>Order Type:</b> {order_type}\n"
    message += f"<b>Stock Symbol:</b> {stock_symbol}\n"

    ws_message = {
        "name": f"{sender_type.capitalize()} G A2",
        "type": order_type,
        "ticker": stock_symbol,
        "sender": sender_type,
    }

    if target:
        ws_message["target"] = target

    await send_ws_message(
        ws_message,
    )
    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    log_message(
        f"Stock alert sent: {stock_symbol} from {sender} ({order_type})", "INFO"
    )


async def run_gmail_scraper():
    service = get_gmail_service()
    last_seen_ids = load_last_alert()

    while True:
        await sleep_until_market_open()
        await initialize_websocket()

        log_message("Market is open. Starting to check for new emails...", "DEBUG")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()

            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                break

            try:
                results = (
                    service.users()
                    .messages()
                    .list(userId="me", labelIds=["INBOX"], maxResults=1)
                    .execute()
                )
                messages = results.get("messages", [])

                if messages and messages[0]["id"] not in last_seen_ids:
                    await process_email(service, messages[0]["id"])
                    last_seen_ids.append(messages[0]["id"])

                    with open(PROCESSED_IDS_FILE, "w") as f:
                        json.dump(last_seen_ids, f, indent=2)
                else:
                    log_message("No new emails found.", "INFO")

                await asyncio.sleep(0.3)

            except HttpError as error:
                if error.resp.status == 429:
                    log_message(
                        "Rate limit hit. Sleeping for 60 seconds...", "CRITICAL"
                    )
                    await asyncio.sleep(60)
                else:
                    log_message(f"An error occurred: {error}", "ERROR")


def main():
    if not all([SCOPES, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_gmail_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
