#!/usr/bin/env python3
"""Telegram bot: search YouTube and cast the top result to the Nest Mini."""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import motion
import phrases
from announce import synthesize_and_serve
from castyt import player

load_dotenv()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

HTTPX_LOG_LEVEL = os.environ.get("HTTPX_LOG_LEVEL", "WARNING").upper()
logging.getLogger("httpx").setLevel(HTTPX_LOG_LEVEL)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}
OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])

ANNOUNCE_MAX_LENGTH = 200


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "CucinaCast: search YouTube and play it on the Nest Mini speaker.\n\n"
        "/play <query> - search YouTube and play the top result "
        "(auto-advances through more results until you /stop); "
        "if you leave out the query, I'll ask for it\n"
        "/announce <text> - speak a custom message on the Nest Mini; "
        "if you leave out the text, I'll ask for it\n"
        "/nowplaying - show the current track\n"
        "/skip - skip to the next track\n"
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


_awaiting_play_text = set()
_awaiting_announce_text = set()


async def play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    user_id = update.effective_user.id
    _awaiting_play_text.discard(user_id)
    _awaiting_announce_text.discard(user_id)

    query = " ".join(context.args) if context.args else None
    if not query:
        _awaiting_play_text.add(user_id)
        await update.message.reply_text("What do you want to play?")
        return

    await _do_play(update, context, query)


async def _do_play(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    await update.message.reply_text(f"Searching for: {query}...")
    try:
        title, url = await asyncio.to_thread(player.play_search, query)
    except Exception as exc:
        logger.exception("Failed to search/cast %r", query)
        await update.message.reply_text(f"Couldn't play that: {exc}")
        return

    await update.message.reply_text(f"Now playing: {title}\n{url}")


async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    user_id = update.effective_user.id
    _awaiting_play_text.discard(user_id)
    _awaiting_announce_text.discard(user_id)

    text = " ".join(context.args) if context.args else None
    if not text:
        _awaiting_announce_text.add(user_id)
        await update.message.reply_text("What should I announce?")
        return

    await _do_announce(update, context, text)


async def _do_announce(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    text = text.strip()
    if not text:
        await update.message.reply_text("Nothing to announce.")
        return
    if len(text) > ANNOUNCE_MAX_LENGTH:
        await update.message.reply_text(
            f"That's too long to announce ({len(text)} chars, max {ANNOUNCE_MAX_LENGTH})."
        )
        return

    try:
        url = await asyncio.to_thread(synthesize_and_serve, text, phrases.tts_lang())
        await asyncio.to_thread(player.announce, url)
    except Exception as exc:
        logger.exception("Failed to announce %r", text)
        await update.message.reply_text(f"Couldn't announce that: {exc}")
        return

    await update.message.reply_text(f"Announcing: {text}")


async def _route_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    user_id = update.effective_user.id
    if user_id in _awaiting_announce_text:
        _awaiting_announce_text.discard(user_id)
        await _do_announce(update, context, update.message.text)
        return
    if user_id in _awaiting_play_text:
        _awaiting_play_text.discard(user_id)
        await _do_play(update, context, update.message.text)
        return
    await _do_play(update, context, update.message.text)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    user_id = update.effective_user.id
    _awaiting_play_text.discard(user_id)
    _awaiting_announce_text.discard(user_id)

    try:
        await asyncio.to_thread(player.stop)
    except Exception as exc:
        logger.exception("Failed to stop casting")
        await update.message.reply_text(f"Couldn't stop: {exc}")
        return

    await update.message.reply_text("Stopped.")


async def nowplaying(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    user_id = update.effective_user.id
    _awaiting_play_text.discard(user_id)
    _awaiting_announce_text.discard(user_id)

    try:
        result = await asyncio.to_thread(player.now_playing)
    except Exception as exc:
        logger.exception("Failed to get now-playing status")
        await update.message.reply_text(f"Couldn't get status: {exc}")
        return

    if result is None:
        await update.message.reply_text("Nothing is playing.")
        return

    title, url, state = result
    suffix = f" ({state})" if state else ""
    await update.message.reply_text(f"Now playing{suffix}: {title}\n{url}")


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    user_id = update.effective_user.id
    _awaiting_play_text.discard(user_id)
    _awaiting_announce_text.discard(user_id)

    try:
        title, url = await asyncio.to_thread(player.skip)
    except Exception as exc:
        logger.exception("Failed to skip track")
        await update.message.reply_text(f"Couldn't skip: {exc}")
        return

    await update.message.reply_text(f"Now playing: {title}\n{url}")


conflict_detected = False


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global conflict_detected
    if isinstance(context.error, Conflict):
        logger.error("Another bot instance is already polling with this token, exiting.")
        conflict_detected = True
        context.application.stop_running()
        return
    logger.error("Unhandled exception while processing update %r", update, exc_info=context.error)


async def _on_motion(category: str) -> None:
    logger.info("Motion detected: %s", category)
    if category == "unknown":
        logger.info("Unclassified motion, skipping announcement")
        return
    text = phrases.announcement_text(category)
    try:
        url = await asyncio.to_thread(synthesize_and_serve, text, phrases.tts_lang())
        await asyncio.to_thread(player.announce, url)
    except Exception:
        logger.exception("Failed to announce motion event")


_motion_task = None


async def post_init(app: Application) -> None:
    global _motion_task
    await app.bot.set_my_commands(
        [
            BotCommand("play", "Search YouTube and play"),
            BotCommand("announce", "Speak a custom message"),
            BotCommand("nowplaying", "Show the current track"),
            BotCommand("skip", "Skip to the next track"),
            BotCommand("stop", "Stop playback"),
        ]
    )
    if motion.motion_detection_enabled():
        # post_init runs before Application.start(), so app.create_task() would
        # warn and not track this task for stop() to await; track it ourselves.
        _motion_task = asyncio.create_task(motion.run_forever(_on_motion))
        logger.info("Motion detection enabled, watching for camera events")
    else:
        logger.info("ONVIF_USER/ONVIF_PASS not set, motion detection disabled")


async def post_stop(app: Application) -> None:
    if _motion_task is not None:
        _motion_task.cancel()
        try:
            await _motion_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("play", play))
    app.add_handler(CommandHandler("announce", announce))
    app.add_handler(CommandHandler("nowplaying", nowplaying))
    app.add_handler(CommandHandler("skip", skip))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _route_text))
    app.add_error_handler(error_handler)
    app.run_polling()
    if conflict_detected:
        sys.exit(1)


if __name__ == "__main__":
    main()
