#!/usr/bin/env python3
"""Telegram bot: search YouTube and cast the top result to the Nest Mini."""
import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from castyt import player

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HTTPX_LOG_LEVEL = os.environ.get("HTTPX_LOG_LEVEL", "WARNING")
logging.getLogger("httpx").setLevel(HTTPX_LOG_LEVEL)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}
OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "CucinaCast: search YouTube and play it on the Nest Mini speaker.\n\n"
        "/play <query> - search YouTube and play the top result "
        "(auto-advances through more results until you /stop)\n"
        "/stop - stop playback\n"
        "/whoami - show your Telegram user id\n\n"
        "You can also just send a plain text message instead of /play."
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram user id: {update.effective_user.id}")


def _is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return update.effective_user.id in ALLOWED_USER_IDS or update.effective_user.id == OWNER_USER_ID


async def _deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"Not authorized to use this bot. Your Telegram user id: {user.id}"
    )
    if OWNER_USER_ID and OWNER_USER_ID != user.id:
        await context.bot.send_message(
            chat_id=OWNER_USER_ID,
            text=f"Unauthorized access attempt from user id {user.id} ({user.full_name}).",
        )


async def play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    query = " ".join(context.args) if context.args else update.message.text
    if not query:
        await update.message.reply_text("Send a song/video name to play, e.g. /play bohemian rhapsody")
        return

    await update.message.reply_text(f"Searching for: {query}...")
    try:
        title, url = await asyncio.to_thread(player.play_search, query)
    except Exception as exc:
        logger.exception("Failed to search/cast %r", query)
        await update.message.reply_text(f"Couldn't play that: {exc}")
        return

    await update.message.reply_text(f"Now playing: {title}\n{url}")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    try:
        await asyncio.to_thread(player.stop)
    except Exception as exc:
        logger.exception("Failed to stop casting")
        await update.message.reply_text(f"Couldn't stop: {exc}")
        return

    await update.message.reply_text("Stopped.")


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("play", "Search YouTube and play on the Nest Mini"),
            BotCommand("stop", "Stop playback"),
        ]
    )


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("play", play))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, play))
    app.run_polling()


if __name__ == "__main__":
    main()
