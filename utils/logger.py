import asyncio
import inspect
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
        main_script = inspect.stack()[-1].filename

        async def send_notification():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            await send_error_notification(message, level, main_script)
            new_loop.close()

        # Run in a separate thread to avoid event loop conflicts
        import threading

        thread = threading.Thread(target=lambda: asyncio.run(send_notification()))
        thread.start()
        thread.join()
