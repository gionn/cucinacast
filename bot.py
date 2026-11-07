#!/usr/bin/env python3
"""Telegram bot: search YouTube and cast the top result to the Nest Mini."""

import asyncio
import logging
import os
import secrets
import sys

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import motion
import navidrome
import phrases
import sounds
import storage
from announce import serve_sound, synthesize_and_serve
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

CALLBACK_PREFIX = "choice:"
MAX_PENDING_CHOICES = 50
BUTTON_LABEL_LENGTH = 60

_choices = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "CucinaCast: play music on the Nest Mini speaker.\n\n"
        "/play <query> - look for albums and tracks on Navidrome first, "
        "then fall back to YouTube; if you leave out the query, I'll ask for it\n"
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


def _shorten(text):
    if len(text) <= BUTTON_LABEL_LENGTH:
        return text
    return text[: BUTTON_LABEL_LENGTH - 1] + "…"


def _album_label(album):
    name = album.get("name") or "Unknown album"
    artist = album.get("artist")
    return _shorten(f"{name} — {artist}" if artist else name)


def _song_label(song):
    title = song.get("title") or "Unknown track"
    artist = song.get("artist")
    return _shorten(f"{title} — {artist}" if artist else title)


def _playing_text(title, url, state=None):
    suffix = f" ({state})" if state else ""
    text = f"Now playing{suffix}: {title}"
    if url:
        text += f"\n{url}"
    return text


def _remember_choice(choice):
    token = secrets.token_hex(6)
    _choices[token] = choice
    while len(_choices) > MAX_PENDING_CHOICES:
        _choices.pop(next(iter(_choices)))
    return token


def _choice_keyboard(query, found):
    rows = []
    for album in found["albums"]:
        token = _remember_choice({"kind": "album", "album": album, "query": query})
        rows.append(
            [InlineKeyboardButton(_album_label(album), callback_data=f"{CALLBACK_PREFIX}{token}")]
        )
    for song in found["songs"]:
        token = _remember_choice({"kind": "song", "song": song, "query": query})
        rows.append(
            [
                InlineKeyboardButton(
                    f"♪ {_song_label(song)}", callback_data=f"{CALLBACK_PREFIX}{token}"
                )
            ]
        )
    token = _remember_choice({"kind": "youtube", "query": query})
    rows.append([InlineKeyboardButton("Search YouTube", callback_data=f"{CALLBACK_PREFIX}{token}")])
    return InlineKeyboardMarkup(rows)


async def _do_play(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    await update.message.reply_text(f"Searching for: {query}...")

    if navidrome.enabled():
        try:
            found = await asyncio.to_thread(navidrome.search, query)
        except navidrome.NavidromeUnavailable as exc:
            logger.warning("Navidrome unavailable, falling back to YouTube: %s", exc)
            found = {"albums": [], "songs": []}
        if found["albums"] or found["songs"]:
            await update.message.reply_text(
                f"Found on Navidrome for: {query}\nPick an album or track:",
                reply_markup=_choice_keyboard(query, found),
            )
            return
        logger.info("No Navidrome match for %r; searching YouTube", query)

    try:
        title, url = await asyncio.to_thread(player.play_search, query)
    except Exception as exc:
        logger.exception("Failed to search/cast %r", query)
        await update.message.reply_text(f"Couldn't play that: {exc}")
        return

    await update.message.reply_text(_playing_text(title, url))


async def _deny_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.callback_query.answer("Not authorized to use this bot.", show_alert=True)
    if OWNER_USER_ID and OWNER_USER_ID != user.id:
        await context.bot.send_message(
            chat_id=OWNER_USER_ID,
            text=f"Unauthorized access attempt from user id {user.id} ({user.full_name}).",
        )


async def _play_youtube_choice(callback, query):
    await callback.edit_message_text(f"Searching YouTube for: {query}...")
    try:
        title, url = await asyncio.to_thread(player.play_search, query)
    except Exception as exc:
        logger.exception("Failed to search/cast %r", query)
        await callback.edit_message_text(f"Couldn't play that: {exc}")
        return
    await callback.edit_message_text(_playing_text(title, url))


async def _play_album_choice(callback, album):
    await callback.edit_message_text(f"Loading album: {_album_label(album)}...")
    try:
        selected = await asyncio.to_thread(navidrome.album_entries, album["id"])
        title, _ = await asyncio.to_thread(
            player.play_entries, selected["entries"], "navidrome", selected["name"]
        )
    except Exception as exc:
        logger.exception("Failed to play album %r", album.get("name"))
        await callback.edit_message_text(f"Couldn't play that album: {exc}")
        return
    await callback.edit_message_text(f"Now playing album: {_album_label(selected)}\n{title}")


async def _play_song_choice(callback, song):
    await callback.edit_message_text(f"Loading: {_song_label(song)}...")
    try:
        entry = await asyncio.to_thread(navidrome.song_entry, song["id"])
        title, url = await asyncio.to_thread(
            player.play_entries, [entry], "navidrome", song.get("title") or "track"
        )
    except Exception as exc:
        logger.exception("Failed to play track %r", song.get("title"))
        await callback.edit_message_text(f"Couldn't play that: {exc}")
        return
    await callback.edit_message_text(_playing_text(title, url))


async def _on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny_callback(update, context)
        return

    callback = update.callback_query
    await callback.answer()
    token = (callback.data or "").partition(CALLBACK_PREFIX)[2]
    choice = _choices.pop(token, None)
    if choice is None:
        await callback.edit_message_text("That selection expired. Send /play again.")
        return

    if choice["kind"] == "youtube":
        await _play_youtube_choice(callback, choice["query"])
    elif choice["kind"] == "album":
        await _play_album_choice(callback, choice["album"])
    elif choice["kind"] == "song":
        await _play_song_choice(callback, choice["song"])


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
    await update.message.reply_text(_playing_text(title, url, state))


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    user_id = update.effective_user.id
    _awaiting_play_text.discard(user_id)
    _awaiting_announce_text.discard(user_id)

    try:
        title, url = await asyncio.to_thread(player.skip)
    except RuntimeError as exc:
        logger.info("Skip failed: %s", exc)
        await update.message.reply_text(f"Couldn't skip: {exc}")
        return
    except Exception as exc:
        logger.exception("Failed to skip track")
        await update.message.reply_text(f"Couldn't skip: {exc}")
        return

    await update.message.reply_text(_playing_text(title, url))


conflict_detected = False


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global conflict_detected
    if isinstance(context.error, Conflict):
        logger.error("Another bot instance is already polling with this token, exiting.")
        conflict_detected = True
        context.application.stop_running()
        return
    logger.error("Unhandled exception while processing update %r", update, exc_info=context.error)


async def _play_motion_sound(category: str) -> None:
    try:
        path = sounds.sound_path(category)
        if path is None:
            logger.info("No sound configured for motion category %r", category)
            return
        url = await asyncio.to_thread(serve_sound, path)
        await asyncio.to_thread(player.announce, url)
    except Exception:
        logger.exception("Failed to play motion sound")


async def _on_motion(category: str) -> None:
    logger.info("Motion detected: %s", category)
    if category == "unknown":
        return
    if phrases.in_quiet_hours():
        logger.info("Quiet hours active, skipping motion sound (%s)", category)
        return
    await _play_motion_sound(category)


_motion_task = None


async def post_init(app: Application) -> None:
    global _motion_task
    await app.bot.set_my_commands(
        [
            BotCommand("play", "Play from Navidrome or YouTube"),
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
    storage.init_db()
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
    app.add_handler(CallbackQueryHandler(_on_choice, pattern=f"^{CALLBACK_PREFIX}"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _route_text))
    app.add_error_handler(error_handler)
    app.run_polling()
    if conflict_detected:
        sys.exit(1)


if __name__ == "__main__":
    main()
