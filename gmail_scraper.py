import asyncio
import base64
import email
import json
import os
import re
from datetime import datetime

import pytz
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TELEGRAM_BOT_TOKEN = os.getenv("GMAIL_SCRAPER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("GMAIL_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")


def get_gmail_service():
    creds = None
    if os.path.exists("cred/token.json"):
        creds = Credentials.from_authorized_user_file("cred/token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "cred/credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open("cred/token.json", "w") as token:
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


def analyze_email_from_oxfordclub(email_body):
    action_to_take = "Action to Take"
    action_index = email_body.find(action_to_take)
    if action_index != -1:
        after_action_text = email_body[action_index + len(action_to_take) :]
        buy_pattern = r"Buy\s+([A-Za-z\s]+)\s*(?:\(\s*[A-Za-z]+:\s*([A-Z]+)\s*\))?"
        match = re.search(buy_pattern, after_action_text)
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
    date_header = get_header(headers, "Date")
    from_email = email.utils.parseaddr(from_header)[1]
    email_body = get_email_body(msg)

    log_message(f"Processing email from: {from_email}", "INFO")
    log_message(f"Subject: {subject}", "INFO")

    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S")
    stock_symbol = None

    if from_email in ["oxford@mp.oxfordclub.com", "oxford@mb.oxfordclub.com"]:
        stock_symbol = analyze_email_from_oxfordclub(email_body)
    elif from_email == "stewie@artoftrading.net":
        stock_symbol = analyze_email_from_artoftrading(subject)
    elif from_email == "do-not-reply@mail.investors.com":
        stock_symbol = analyze_email_from_investors(subject)

    if stock_symbol:
        await send_stock_alert(timestamp, from_email, stock_symbol)


async def send_stock_alert(timestamp, sender, stock_symbol):
    message = f"<b>New Stock Alert</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Sender:</b> {sender}\n"
    message += f"<b>Stock Symbol:</b> {stock_symbol}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    await send_ws_message(
        {"sender": "Gmail", "type": "Stock Alert", "content": message}, WS_SERVER_URL
    )
    log_message(f"Stock alert sent: {stock_symbol} from {sender}", "INFO")


async def main():
    service = get_gmail_service()
    last_seen_id = None

    while True:
        await sleep_until_market_open()
        log_message("Market is open. Starting to check for new emails...")

        while True:
            current_time = datetime.now(pytz.timezone("America/New_York"))
            _, _, market_close_time = get_next_market_times()

            if current_time > market_close_time:
                log_message("Market is closed. Waiting for next market open...")
                break

            try:
                results = (
                    service.users()
                    .messages()
                    .list(userId="me", labelIds=["INBOX"], maxResults=1)
                    .execute()
                )
                messages = results.get("messages", [])

                if messages and messages[0]["id"] != last_seen_id:
                    await process_email(service, messages[0]["id"])
                    last_seen_id = messages[0]["id"]
                else:
                    log_message("No new emails found.", "INFO")

                await asyncio.sleep(5)  # Check every 5 seconds

            except HttpError as error:
                if error.resp.status == 429:
                    log_message("Rate limit hit. Sleeping for 60 seconds...", "WARNING")
                    await asyncio.sleep(60)
                else:
                    log_message(f"An error occurred: {error}", "ERROR")


if __name__ == "__main__":
    asyncio.run(main())
