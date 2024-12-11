import ssl

import aiohttp
from utils.logger import log_message


async def send_telegram_message(message, bot_token, chat_id):
    """
    Sends a message to a Telegram chat asynchronously.

    :param message: The message to send
    :param bot_token: The bot token for the specific Telegram bot
    :param chat_id: The chat ID to send the message to
    :return: The response from the Telegram API
    """
    if not bot_token or not chat_id:
        raise ValueError("Bot token and chat ID must be provided.")

    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                telegram_url, json=payload, ssl=ssl_context
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    error_message = await response.text()
                    raise Exception(f"Failed to send message: {error_message}")
    except Exception as e:
        log_message(f"Error sending message to telegram: {e}", "ERROR")
        return None
