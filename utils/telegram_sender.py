import io
import ssl
from datetime import datetime

import aiohttp
import pytz

from utils.base_logger import setup_logger


async def send_telegram_message(
    message, bot_token, chat_id, file_content=None, filename=None
):
    """
    Sends a message to a Telegram chat asynchronously, optionally with a file.
    """
    if not bot_token or not chat_id:
        raise ValueError("Bot token and chat ID must be provided.")

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with aiohttp.ClientSession() as session:
            # First send the message
            message_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}

            async with session.post(
                message_url, json=payload, ssl=ssl_context
            ) as response:
                if response.status != 200:
                    error_message = await response.text()
                    raise Exception(f"Failed to send message: {error_message}")

            # If file content is provided, send it as a document
            if file_content and filename:
                file_url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
                form_data = aiohttp.FormData()
                form_data.add_field("chat_id", str(chat_id))
                form_data.add_field(
                    "document", io.StringIO(file_content), filename=filename
                )

                async with session.post(
                    file_url, data=form_data, ssl=ssl_context
                ) as file_response:
                    if file_response.status != 200:
                        error_message = await file_response.text()
                        raise Exception(f"Failed to send file: {error_message}")

            return True

    except Exception as e:
        message = f"Error sending message to telegram: {e}"
        timestamp = datetime.now(pytz.timezone("US/Eastern")).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        logger = setup_logger()
        getattr(logger, "critical")(f"[{timestamp}] {message}")
        return None
