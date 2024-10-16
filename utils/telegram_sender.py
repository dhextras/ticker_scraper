import requests


def send_telegram_message(message, bot_token, chat_id):
    """
    Sends a message to a Telegram chat.

    :param message: The message to send
    :param bot_token: The bot token for the specific Telegram bot
    :param chat_id: The chat ID to send the message to
    :return: The response from the Telegram API
    """
    if not bot_token or not chat_id:
        raise ValueError("Bot token and chat ID must be provided.")

    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    response = requests.post(telegram_url, data=payload)
    return response.json()
