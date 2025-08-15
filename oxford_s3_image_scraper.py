import asyncio
import json
import os
import subprocess
from typing import Set

from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)

load_dotenv()

# Constants
CHECK_INTERVAL = 300  # 5 minutes
SYNC_INTERVAL = 300  # 5 minutes
TELEGRAM_BOT_TOKEN = os.getenv("OXFORDCLUB_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("OXFORDCLUB_TELEGRAM_GRP")
LOCAL_IMAGE_FOLDER = "data/oxford_images"
PROCESSED_JSON_FILE = "data/oxford_processed_image_files.json"

os.makedirs(LOCAL_IMAGE_FOLDER, exist_ok=True)
os.makedirs("data", exist_ok=True)


def check_aws_credentials():
    """Check if AWS credentials are configured."""
    aws_cred_path = os.path.expanduser("~/.aws/credentials")
    if not os.path.exists(aws_cred_path):
        log_message(
            "AWS credentials not found. Please configure AWS first.", "CRITICAL"
        )
        return False
    return True


def load_processed_files() -> Set[str]:
    """Load processed file names from JSON."""
    try:
        with open(PROCESSED_JSON_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_files(files: Set[str]):
    """Save processed file names to JSON."""
    with open(PROCESSED_JSON_FILE, "w") as f:
        json.dump(list(files), f, indent=2)


def generate_date_patterns():
    """Generate all possible date formats with wildcard variations."""
    current_date = get_current_time()

    patterns = set()

    formats = [
        "%Y-%m-%d",
        "%Y%m%d",
        "%y%m%d",
        "%m%d%y",
        "%m%d%Y",
        "%m-%d-%Y",
        "%m-%d-%y",
        "%m%d",
        "%m-%d",
        "%d%m%y",
        "%d%m%Y",
        "%d-%m-%Y",
        "%d-%m-%y",
    ]

    for fmt in formats:
        date_str = current_date.strftime(fmt)
        patterns.add(f"*{date_str}*")
        patterns.add(f"{date_str}*")

    return sorted(patterns)


def generate_sync_command():
    cmd = [
        "aws",
        "s3",
        "sync",
        "s3://assets.oxfordclub.com/emails/images",
        LOCAL_IMAGE_FOLDER,
        "--exclude",
        "*",
    ]
    for p in generate_date_patterns():
        cmd.extend(["--include", p])
    return cmd


async def sync_s3_files():
    """Sync files from S3 bucket."""
    try:
        # NOTE: You must be logged into AWS first for this to work
        cmd = generate_sync_command()
        log_message(
            f"Running AWS sync command, prolly take a shit load of time....", "INFO"
        )

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode == 0:
            log_message("AWS S3 sync completed successfully", "INFO")
            return True
        else:
            log_message(f"AWS S3 sync failed: {result.stderr}", "ERROR")
            return False

    except subprocess.TimeoutExpired:
        log_message("AWS S3 sync timed out", "ERROR")
        return False
    except Exception as e:
        log_message(f"Error during S3 sync: {e}", "ERROR")
        return False


def get_current_files() -> Set[str]:
    """Get current files in the local folder."""
    try:
        files = set()
        for root, dirs, filenames in os.walk(LOCAL_IMAGE_FOLDER):
            for filename in filenames:
                if filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp")):
                    relative_path = os.path.relpath(
                        os.path.join(root, filename), LOCAL_IMAGE_FOLDER
                    )
                    files.add(relative_path)
        return files
    except Exception as e:
        log_message(f"Error getting current files: {e}", "ERROR")
        return set()


async def send_new_files_to_telegram(new_files: Set[str]):
    """Send notification about new files to Telegram."""
    if not new_files:
        return

    timestamp = get_current_time().strftime("%Y-%m-%d %H:%M:%S")

    message = f"<b>New Oxford Club S3 Images Found</b>\n\n"
    message += f"<b>Time:</b> {timestamp}\n"
    message += f"<b>Count:</b> {len(new_files)}\n\n"
    message += f"<b>Files:</b>\n"

    for file in sorted(new_files):
        message += f"    - https://s3.amazonaws.com/assets.oxfordclub.com/emails/images/{file}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Sent {len(new_files)} new files to Telegram", "INFO")


async def run_scraper():
    """Main scraper loop."""
    if not check_aws_credentials():
        return

    processed_files = load_processed_files()

    while True:
        await sleep_until_market_open()

        log_message("Market is open. Starting Oxford image sync...", "DEBUG")
        _, _, market_close_time = get_next_market_times()

        while True:
            current_time = get_current_time()

            if current_time > market_close_time:
                log_message(
                    "Market is closed. Waiting for next market open...", "DEBUG"
                )
                break

            sync_success = await sync_s3_files()

            if sync_success:
                current_files = get_current_files()
                new_files = current_files - processed_files

                if new_files:
                    # await send_new_files_to_telegram(new_files)
                    processed_files.update(new_files)
                    save_processed_files(processed_files)
                    log_message(f"Found {len(new_files)} new files", "INFO")
                else:
                    log_message("No new files found", "DEBUG")

            await asyncio.sleep(SYNC_INTERVAL)


def main():
    """Main function."""
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        return

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")


if __name__ == "__main__":
    main()
