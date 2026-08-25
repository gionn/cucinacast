# CucinaCast

Telegram bot that searches YouTube and casts the top result to a Nest Mini (Chromecast).

## Setup

```
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

Environment variables:

- `TELEGRAM_BOT_TOKEN` — required, from @BotFather.
- `NEST_DEVICE_NAME` — Chromecast friendly name to cast to (e.g. `Cucinino`). If unset,
  `catt`'s configured default device is used.
- `OWNER_USER_ID` — required, your Telegram user id (use `/whoami` in the bot to find it).
  Gets notified whenever an unauthorized user tries `/play` or `/stop`, and is always
  allowed to use the bot regardless of `ALLOWED_USER_IDS`.
- `ALLOWED_USER_IDS` — optional, comma-separated Telegram user ids allowed to use
  `/play`/`/stop`. If unset, anyone can use the bot.

## Try casting standalone

```
NEST_DEVICE_NAME="Cucinino" ./venv/bin/python cast_test.py "some song name"
```

## Run the bot

```
TELEGRAM_BOT_TOKEN="..." NEST_DEVICE_NAME="Cucinino" ./venv/bin/python bot.py
```

Send `/play <query>` or any plain text message to the bot in Telegram.

## Known limitation

Live YouTube streams (e.g. 24/7 lofi radio streams) resolve to an HLS manifest whose
content type `yt-dlp`/`catt` can't always detect, which the Nest Mini fails to play.
Regular (non-live) videos work reliably.
