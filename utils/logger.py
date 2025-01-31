import asyncio
from datetime import datetime

import pytz

from utils.base_logger import setup_logger
from utils.error_notifier import send_error_notification


def log_message(message, level="INFO"):
    timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3]
    logger = setup_logger()
    getattr(logger, level.lower())(f"[{timestamp}] {message}")

    if level.upper() != "INFO":
        asyncio.run(send_error_notification(message, level))
