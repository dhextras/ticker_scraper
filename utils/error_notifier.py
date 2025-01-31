import inspect
import os
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


async def send_error_notification(message, level="WARNING"):
    if not all([ERROR_NOTIFY_BOT_TOKEN, ERROR_NOTIFY_GRP]):
        raise ValueError(
            "Missing required environment variables for error notifications"
        )

    main_script = inspect.stack()[-1].filename
    script_name = os.path.splitext(os.path.basename(main_script))[0]

    current_time = datetime.now(pytz.timezone("US/Eastern"))
    date = current_time.strftime("%Y/%m")
    day = current_time.strftime("%d")

    log_file = os.path.join("log", date, script_name, f"{day}.log")

    # Trim message if it contains newlines or exceeds 300 chars
    if "\n" in message:
        message = message.split("\n")[0] + ".."
    elif len(message) > 300:
        message = message[:300] + "..."

    emoji = LEVEL_EMOJIS.get(level.upper(), "ℹ️")
    alert_message = f"{emoji} <b>Error Notifier -  {level.upper()}</b> {emoji}\n\n"
    alert_message += f"<b>Script:</b> {script_name}\n"
    alert_message += f"<b>Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    alert_message += f"<b>Message:</b> {message}\n"
    alert_message += f"\n<b><i>Last 15 lines of logs attached below...</i></b>"

    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            lines = f.readlines()
            last_15_lines = lines[-15:] if len(lines) >= 15 else lines
            log_content = "".join(last_15_lines)
        await send_telegram_message(
            alert_message,
            ERROR_NOTIFY_BOT_TOKEN,
            ERROR_NOTIFY_GRP,
            file_content=log_content,
            filename=f"last_15_lines_of-{script_name}-{date.replace('/', '_')}_{day}.log",
        )
    else:
        await send_telegram_message(
            alert_message, ERROR_NOTIFY_BOT_TOKEN, ERROR_NOTIFY_GRP
        )
