#!/usr/bin/env python3
"""Telegram bot: search YouTube and cast the top result to the Nest Mini."""

import asyncio
import logging
import os
import sys
import time

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import motion
import phrases
import presence
import storage_bluetooth
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
        "/whoami - show your Telegram user id\n"
        "/athome - who is home (Bluetooth presence)\n"
        "/adddevice - register a Bluetooth device to track\n"
        "/rmdevice <nickname|mac> - stop tracking a device\n\n"
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
_awaiting_device_pick = {}
_awaiting_device_nickname = {}
_pair_sessions = {}

_CANCEL_WORDS = {"cancel", "abort", "stop"}


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
    if update.message.text.strip().lower() in _CANCEL_WORDS:
        if user_id in _pair_sessions:
            await _answer_pair_confirm(update, context)
            return
        if user_id in _awaiting_device_pick or user_id in _awaiting_device_nickname:
            _awaiting_device_pick.pop(user_id, None)
            _awaiting_device_nickname.pop(user_id, None)
            await update.message.reply_text("Device setup cancelled.")
            return
    if user_id in _awaiting_device_pick:
        await _finish_device_pick(update, context)
        return
    if user_id in _awaiting_device_nickname:
        await _save_nickname(update, context)
        return
    if user_id in _pair_sessions:
        await _answer_pair_confirm(update, context)
        return
    if user_id in _awaiting_announce_text:
        _awaiting_announce_text.discard(user_id)
        await _do_announce(update, context, update.message.text)
        return
    if user_id in _awaiting_play_text:
        _awaiting_play_text.discard(user_id)
        await _do_play(update, context, update.message.text)
        return
    await _do_play(update, context, update.message.text)


async def adddevice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    user_id = update.effective_user.id
    _awaiting_play_text.discard(user_id)
    _awaiting_announce_text.discard(user_id)
    _awaiting_device_pick.pop(user_id, None)
    _awaiting_device_nickname.pop(user_id, None)
    _pair_sessions.pop(user_id, None)

    if not presence.bluetooth_available():
        await update.message.reply_text(
            "Bluetooth presence is disabled: no usable Bluetooth adapter was found "
            "at startup. See the README for setup and restart the bot."
        )
        return

    args = context.args or []
    if args:
        try:
            mac = storage_bluetooth.normalize_mac(args[0])
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        nickname = " ".join(args[1:]).strip()
        if nickname:
            await _pair_and_store(update, context, mac, nickname)
            return
        _awaiting_device_nickname[user_id] = {"mac": mac, "name": mac}
        await update.message.reply_text(f"What nickname should I use for {mac}?")
        return

    await update.message.reply_text(
        "Scanning for nearby Bluetooth devices for "
        f"{presence._discovery_timeout_seconds()} seconds..."
    )
    try:
        devices = await presence.discover_devices()
    except Exception as exc:
        logger.exception("Bluetooth discovery failed")
        await update.message.reply_text(f"Discovery failed: {exc}")
        return
    known = {device["mac"] for device in storage_bluetooth.list_devices()}
    candidates = [device for device in devices if device["mac"] not in known]
    if not candidates:
        await update.message.reply_text(
            "No new devices found. Make sure the phone's Bluetooth is on and it's "
            "discoverable (e.g. Bluetooth settings open with 'Pair new device'), "
            "then try /adddevice again."
        )
        return
    _awaiting_device_pick[user_id] = {"devices": candidates}
    lines = [
        f"{i}. {device['name'] or device['mac']} ({device['mac']})"
        for i, device in enumerate(candidates)
    ]
    await update.message.reply_text(
        "Which device is this? Reply with a number:\n" + "\n".join(lines)
    )


async def _finish_device_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    state = _awaiting_device_pick.pop(user_id, None)
    if state is None:
        return
    try:
        index = int(update.message.text.strip())
        if not 0 <= index < len(state["devices"]):
            raise IndexError
        device = state["devices"][index]
    except (ValueError, IndexError):
        await update.message.reply_text("That's not a valid number. Send /adddevice to start over.")
        return
    _awaiting_device_nickname[user_id] = device
    await update.message.reply_text(
        f"What nickname should I use for {device['name'] or device['mac']}?"
    )


async def _pair_and_store(
    update: Update, context: ContextTypes.DEFAULT_TYPE, mac: str, nickname: str, display_name=None
) -> None:
    user_id = update.effective_user.id
    name = display_name or mac
    if mac in {device["mac"] for device in storage_bluetooth.list_devices()}:
        await update.message.reply_text(f"{name} is already registered.")
        return
    await update.message.reply_text(f"Pairing with {name}...")

    async def on_prompt(passkey):
        future = asyncio.get_running_loop().create_future()
        _pair_sessions[user_id] = future
        await update.message.reply_text(
            f"Passkey {passkey}. Confirm it on your phone. I'll auto-confirm in "
            "15 seconds — reply 'no' to cancel."
        )
        try:
            reply = await asyncio.wait_for(future, timeout=15)
        except asyncio.TimeoutError:
            reply = "yes"
        finally:
            _pair_sessions.pop(user_id, None)
        return reply

    ok, message = await presence.pair_device(mac, on_prompt)
    if not ok:
        await update.message.reply_text(f"Pairing failed: {message}")
        return
    if not storage_bluetooth.add_device(mac, nickname):
        await update.message.reply_text(f"{name} is already registered.")
        return
    await update.message.reply_text(
        f"Added {nickname} ({mac}). I'll track their presence from now on."
    )


async def _save_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    device = _awaiting_device_nickname.pop(user_id, None)
    if device is None:
        return
    nickname = update.message.text.strip()
    if not nickname:
        await update.message.reply_text(
            "The nickname can't be empty. Send /adddevice to try again."
        )
        return
    await _pair_and_store(update, context, device["mac"], nickname, display_name=device["name"])


async def _answer_pair_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    future = _pair_sessions.get(user_id)
    if future is None or future.done():
        return
    reply = update.message.text.strip().lower()
    if reply in ("no", "n") or reply in _CANCEL_WORDS:
        future.set_result(None)
    elif reply in ("yes", "y", "ok", "confirm"):
        future.set_result("yes")
    else:
        await update.message.reply_text("Reply 'no' to cancel the pairing.")


def _format_last_seen(timestamp):
    seconds = time.time() - timestamp
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} h ago"
    return f"{int(hours // 24)} d ago"


async def athome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    user_id = update.effective_user.id
    _awaiting_play_text.discard(user_id)
    _awaiting_announce_text.discard(user_id)

    devices = storage_bluetooth.list_devices()
    if not devices:
        await update.message.reply_text(
            "No devices registered. Use /adddevice to track who's home."
        )
        return
    lines = []
    groups = {}
    for device in devices:
        groups.setdefault(device["nickname"], []).append(device)
    for nickname, group in groups.items():
        home_devices = [device for device in group if device["home"]]
        away_devices = [device for device in group if not device["home"]]
        lines.append(f"{nickname}: {'home' if home_devices else 'away'}")
        for device in home_devices:
            lines.append(f"  • {device['mac']}: home")
        for device in away_devices:
            if device["last_seen"] is not None:
                lines.append(
                    f"  • {device['mac']}: away (last seen "
                    f"{_format_last_seen(device['last_seen'])})"
                )
            else:
                lines.append(f"  • {device['mac']}: away (never seen)")
    await update.message.reply_text("\n".join(lines))


async def rmdevice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return

    user_id = update.effective_user.id
    _awaiting_play_text.discard(user_id)
    _awaiting_announce_text.discard(user_id)

    query = " ".join(context.args).strip().lower()
    if not query:
        await update.message.reply_text("Usage: /rmdevice <nickname or mac>")
        return
    try:
        normalized = storage_bluetooth.normalize_mac(query)
    except ValueError:
        normalized = None
    for device in storage_bluetooth.list_devices():
        if (
            device["nickname"].lower() == query
            or device["mac"] == normalized
            or device["mac"].lower() == query
        ):
            storage_bluetooth.remove_device(device["mac"])
            await update.message.reply_text(f"Removed {device['nickname']} ({device['mac']}).")
            return
    await update.message.reply_text(f"No device matching {query!r}.")


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
    except RuntimeError as exc:
        logger.info("Skip failed: %s", exc)
        await update.message.reply_text(f"Couldn't skip: {exc}")
        return
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


async def _do_announce_motion(text: str) -> None:
    try:
        url = await asyncio.to_thread(synthesize_and_serve, text, phrases.tts_lang())
        await asyncio.to_thread(player.announce, url)
    except Exception:
        logger.exception("Failed to announce motion event")


async def _send_motion_clip(caption: str) -> None:
    path = None
    try:
        path = await motion.capture_clip()
        with open(path, "rb") as clip:
            await _bot.send_video(chat_id=OWNER_USER_ID, video=clip, caption=caption)
    except Exception:
        logger.exception("Failed to capture/send motion clip")
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


_background_tasks = set()


async def _on_motion(category: str) -> None:
    logger.info("Motion detected: %s", category)
    if category == "unknown":
        logger.info("Unclassified motion, skipping announcement")
        return
    if phrases.in_quiet_hours():
        logger.info("Quiet hours active, skipping motion announcement (%s)", category)
        return
    text = phrases.announcement_text(category)
    if _video_clips_enabled:
        # Run in the background rather than awaiting: a slow Telegram upload
        # would otherwise delay motion.py's caller from returning, extending
        # the real debounce window past DEBOUNCE_SECONDS.
        task = asyncio.create_task(_send_motion_clip(text))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    await _do_announce_motion(text)


_motion_task = None
_presence_task = None
_bot = None
_video_clips_enabled = False


async def _on_presence_transition(transition) -> None:
    nickname = transition["nickname"]
    state = "home" if transition["home"] else "away"
    logger.info("Presence transition: %s is now %s", nickname, state)
    try:
        await _bot.send_message(chat_id=OWNER_USER_ID, text=f"{nickname} is now {state}.")
    except Exception:
        logger.exception("Failed to notify owner of presence transition")


async def post_init(app: Application) -> None:
    global _motion_task, _bot, _video_clips_enabled, _presence_task
    _bot = app.bot
    await app.bot.set_my_commands(
        [
            BotCommand("play", "Search YouTube and play"),
            BotCommand("announce", "Speak a custom message"),
            BotCommand("nowplaying", "Show the current track"),
            BotCommand("skip", "Skip to the next track"),
            BotCommand("stop", "Stop playback"),
            BotCommand("athome", "Who is home (Bluetooth presence)"),
            BotCommand("adddevice", "Register a Bluetooth device to track"),
            BotCommand("rmdevice", "Stop tracking a device"),
        ]
    )
    if presence.bluetooth_available():
        _presence_task = asyncio.create_task(presence.run_forever(_on_presence_transition))
        logger.info("Bluetooth presence tracking enabled")
    else:
        logger.info("No Bluetooth adapter found, presence tracking disabled")
    if motion.motion_detection_enabled():
        _video_clips_enabled = motion.ffmpeg_available()
        if not _video_clips_enabled:
            logger.warning(
                "ffmpeg not found on PATH, motion clips disabled (audio announcements still work)"
            )
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
    if _presence_task is not None:
        _presence_task.cancel()
        try:
            await _presence_task
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
    app.add_handler(CommandHandler("athome", athome))
    app.add_handler(CommandHandler("adddevice", adddevice))
    app.add_handler(CommandHandler("rmdevice", rmdevice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _route_text))
    app.add_error_handler(error_handler)
    app.run_polling()
    if conflict_detected:
        sys.exit(1)


if __name__ == "__main__":
    main()
