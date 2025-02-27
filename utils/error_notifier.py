import asyncio
import inspect
import os
import time
from collections import defaultdict
from datetime import datetime

import pytz
from dotenv import load_dotenv

from utils.telegram_sender import send_telegram_message

load_dotenv()

ERROR_NOTIFY_BOT_TOKEN = os.getenv("ERROR_NOTIFY_BOT_TOKEN")
ERROR_NOTIFY_GRP = os.getenv("ERROR_NOTIFY_GRP")
LEVEL_EMOJIS = {
    "DEBUG": "🔍",
    "WARNING": "ℹ️",
    "ERROR": "❌",
    "CRITICAL": "🔥",
}

warning_counts = defaultdict(
    lambda: defaultdict(int)
)  # {script_name: {message: count}}
last_reset_time = time.time()
reset_task = None
warning_threshold = 5
reset_interval = 300  # 5 minutes


async def reset_warning_counts_task():
    global warning_counts, last_reset_time
    while True:
        await asyncio.sleep(reset_interval)
        warning_counts = defaultdict(lambda: defaultdict(int))
        last_reset_time = time.time()


def ensure_reset_task_running():
    global reset_task
    if reset_task is None or reset_task.done():
        loop = asyncio.get_event_loop()
        reset_task = loop.create_task(reset_warning_counts_task())


async def send_error_notification(message, level="WARNING", main_script=None):
    if not all([ERROR_NOTIFY_BOT_TOKEN, ERROR_NOTIFY_GRP]):
        raise ValueError(
            "Missing required environment variables for error notifications"
        )

    ensure_reset_task_running()
    file_needed = True if level.upper() in ["ERROR", "CRITICAL"] else False

    if main_script is None:
        main_script = inspect.stack()[-1].filename

    script_name = os.path.splitext(os.path.basename(main_script))[0]
    current_time = datetime.now(pytz.timezone("America/Chicago"))
    date = current_time.strftime("%Y/%m")
    day = current_time.strftime("%d")

    log_file = os.path.join("log", date, script_name, f"{day}.log")

    # Trim message if it contains newlines or exceeds 300 chars
    if "\n" in message:
        message = message.split("\n")[0] + ".."
    elif len(message) > 300:
        message = message[:300] + "..."

    emoji = LEVEL_EMOJIS.get(level.upper(), "ℹ️")

    if level.upper() == "WARNING":
        warning_counts[script_name][message] += 1

        if warning_counts[script_name][message] == warning_threshold:
            alert_message = f"{emoji} <b>Error Notifier (Repeated) -  {level.upper()}</b> {emoji}\n\n"
            alert_message += f"<b>Script:</b> {script_name}\n"
            alert_message += (
                f"<b>Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            )

            alert_message += (
                f"<b>Message (repeated {warning_threshold} times):</b> {message}\n"
            )

            await send_telegram_message(
                alert_message, ERROR_NOTIFY_BOT_TOKEN, ERROR_NOTIFY_GRP
            )

            warning_counts[script_name][message] = 0

        # Don't send individual notifications for warnings
        return

    alert_message = f"{emoji} <b>Error Notifier -  {level.upper()}</b> {emoji}\n\n"
    alert_message += f"<b>Script:</b> {script_name}\n"
    alert_message += f"<b>Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    alert_message += f"<b>Message:</b> {message}\n"

    if file_needed:
        alert_message += f"\n<b><i>Last 15 lines of the logs attached below...</i></b>"

    if file_needed and os.path.exists(log_file):
        with open(log_file, "r") as f:
            lines = f.readlines()
            last_15_lines = lines[-15:] if len(lines) >= 15 else lines
            log_content = "".join(last_15_lines)
        await send_telegram_message(
            alert_message,
            ERROR_NOTIFY_BOT_TOKEN,
            ERROR_NOTIFY_GRP,
            file_content=log_content,
            filename=f"{script_name}-{date.replace('/', '_')}_{day}.log",
        )
    else:
        await send_telegram_message(
            alert_message, ERROR_NOTIFY_BOT_TOKEN, ERROR_NOTIFY_GRP
        )
