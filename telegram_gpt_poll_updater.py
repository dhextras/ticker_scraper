import asyncio
import json
import os

import aiohttp
from dotenv import load_dotenv

from utils.logger import log_message

load_dotenv()

GPT_NOTIFY_BOT_TOKEN = os.getenv("GPT_NOTIFY_BOT_TOKEN")


class TelegramBot:
    def __init__(self, bot_token):
        self.bot_token = bot_token
        self.last_update_id = 0

    async def handle_callback_query(self, callback_query):
        """Handle button clicks"""
        try:
            callback_data = callback_query["data"]
            message = callback_query["message"]
            user = callback_query["from"]
            callback_id = callback_query["id"]

            log_message(
                f"Button clicked: {callback_data} by {user.get('username', user.get('first_name'))}",
                "INFO",
            )

            if callback_data.startswith("rate_good_") or callback_data.startswith(
                "rate_bad_"
            ):
                is_good = callback_data.startswith("rate_good_")
                rating_text = "✅ GOOD" if is_good else "❌ POOR"

                username = user.get("username", "")
                first_name = user.get("first_name", "")
                user_display = f"@{username}" if username else first_name

                original_text = message["text"]
                rating_info = f"\n\n<b>Rating:</b> {rating_text} (by {user_display})"
                updated_text = original_text + rating_info

                await self.edit_message(
                    chat_id=message["chat"]["id"],
                    message_id=message["message_id"],
                    text=updated_text,
                    reply_markup=None,
                )

                await self.answer_callback_query(
                    callback_id, f"Thanks! Rated as {rating_text}"
                )

                log_message(f"Analysis rated {rating_text} by {user_display}", "INFO")

        except Exception as e:
            log_message(f"Error handling callback: {e}", "ERROR")

    async def edit_message(self, chat_id, message_id, text, reply_markup=None):
        """Edit a message"""
        url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"

        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }

        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup) if reply_markup else ""

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    error = await response.text()
                    log_message(f"Failed to edit message: {error}", "ERROR")
                    return False
                return True

    async def answer_callback_query(self, callback_query_id, text=""):
        """Send acknowledgment for button click"""
        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"

        payload = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": False,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                return response.status == 200

    async def get_updates(self):
        """Get updates from Telegram"""
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params = {"offset": self.last_update_id + 1, "timeout": 30}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("result", [])
        except Exception as e:
            log_message(f"Error getting updates: {e}", "ERROR")

        return []

    async def start_polling(self):
        """Start polling for updates"""
        log_message(f"Bot started! Listening for button clicks...", "DEBUG")

        while True:
            try:
                updates = await self.get_updates()

                for update in updates:
                    self.last_update_id = update["update_id"]

                    if "callback_query" in update:
                        await self.handle_callback_query(update["callback_query"])

            except Exception as e:
                log_message(f"Polling error: {e}", "ERROR")
                await asyncio.sleep(5)


async def main():
    if not GPT_NOTIFY_BOT_TOKEN:
        log_message("GPT_NOTIFY_BOT_TOKEN not found!", "CRITICAL")
        return

    bot = TelegramBot(GPT_NOTIFY_BOT_TOKEN)
    await bot.start_polling()


if __name__ == "__main__":
    asyncio.run(main())
