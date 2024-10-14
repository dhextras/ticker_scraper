import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

DEFAULT_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DEFAULT_GROUP_CHAT_ID = os.environ.get("TELEGRAM_GROUP_CHAT_ID")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("I'm a bot, please talk to me!")


async def send_to_group(message: str, bot_token=None, group_chat_id=None) -> None:
    bot_token = bot_token or DEFAULT_BOT_TOKEN
    group_chat_id = group_chat_id or DEFAULT_GROUP_CHAT_ID

    if not bot_token or not group_chat_id:
        raise ValueError("Bot token and group chat ID must be provided.")

    app = Application.builder().token(bot_token).build()
    await app.bot.send_message(chat_id=group_chat_id, text=message, parse_mode="HTML")


async def listen_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_message = update.message.text  # Get the text message from the user
    await update.message.reply_text(f"You said: {user_message}")
    print(update.message)


def run_bot(bot_token=None, group_chat_id=None):
    bot_token = bot_token or DEFAULT_BOT_TOKEN
    group_chat_id = group_chat_id or DEFAULT_GROUP_CHAT_ID

    if not bot_token:
        raise ValueError("Bot token must be provided to run the bot.")

    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, listen_messages))
    print("Telegram bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run_bot()
