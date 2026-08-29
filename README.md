# CucinaCast

Telegram bot that searches YouTube and casts the top result to a Nest Mini (Chromecast).

Named after "cucina" (Italian for kitchen) — the Nest Mini it talks to lives in the
kitchen.

## Setup

```
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Create a `.env` file in the project root (loaded automatically by `bot.py` via
`python-dotenv`):

```
TELEGRAM_BOT_TOKEN=
NEST_DEVICE_NAME=Cucinino
OWNER_USER_ID=
ALLOWED_USER_IDS=
```

Environment variables:

Bot / casting:

- `TELEGRAM_BOT_TOKEN` — required, from @BotFather.
- `NEST_DEVICE_NAME` — Chromecast friendly name to cast to (e.g. `Cucinino`, found via
  `./.venv/bin/catt scan`). If unset, `catt`'s configured default device is used.
- `OWNER_USER_ID` — required, your Telegram user id (send `/whoami` to the bot to find
  it). Always allowed to use the bot regardless of `ALLOWED_USER_IDS`, and gets a
  Telegram message whenever another user's `/play` or `/stop` is denied (with that
  user's id and name), so you can decide whether to add them to `ALLOWED_USER_IDS`.
- `ALLOWED_USER_IDS` — optional, comma-separated Telegram user ids allowed to use
  `/play`/`/stop` in addition to the owner. If unset, anyone can use the bot.
- `LOG_LEVEL` — optional, overall log level for the bot (default `INFO`).
- `HTTPX_LOG_LEVEL` — optional, log level for the `httpx` library (used internally by
  `python-telegram-bot` for polling). Defaults to `WARNING` to avoid flooding the logs
  with a line per poll request; set to `INFO` or `DEBUG` for verbose HTTP logging.

Motion detection / doorbell announcements (see below — all optional, and the
feature is entirely disabled unless `ONVIF_USER` and `ONVIF_PASS` are both set):

- `ONVIF_USER` / `ONVIF_PASS` — ONVIF camera credentials. If either is unset,
  motion detection is disabled and the bot behaves exactly as without a camera.
- `ONVIF_HOST` / `ONVIF_PORT` — the camera's address. If `ONVIF_HOST` is unset, the
  camera is auto-discovered via WS-Discovery on the LAN.
- `ANNOUNCE_PORT` — local port used to serve TTS announcement audio to the
  Chromecast (default `8765`).
- `ANNOUNCE_HOST` — override the LAN IP advertised to the Chromecast for fetching
  announcement audio (auto-detected by default; only needed on multi-NIC
  machines).
- `TTS_LANG` — language for announcement speech and wording (default `en`; `it` is
  also supported). Any other `gTTS`-supported language code works for speech, but
  the announcement wording itself is only translated for `en`/`it` — falls back to
  English wording for other codes.

## Run the bot

```
./.venv/bin/python bot.py
```

## Run as a systemd service

After cloning the repo, run:

```
./setup.sh
```

This creates the venv, installs dependencies, scaffolds a `.env` (if missing), and
installs/enables a system-wide `cucinacast.service` unit (requires `sudo`) running as
the user who cloned the repo. Fill in `.env`, then:

```
sudo systemctl start cucinacast
sudo journalctl -u cucinacast -f
```

## Bot commands

- `/start` — explains what the bot does and lists the commands below.
- `/play <query>` — search YouTube and play the top result on the Nest Mini. Also
  triggered by sending any plain text message (no need for the `/play` prefix). Queues
  the top search results and auto-advances to the next one when the current track
  finishes; when the queue runs out it fetches more results for the same query and
  keeps going, indefinitely, until `/stop` is sent. Unplayable results (private/removed
  videos) are skipped automatically.
- `/stop` — stop playback and clear the queue.
- `/whoami` — reply with your Telegram user id, to put in `OWNER_USER_ID` /
  `ALLOWED_USER_IDS`.

`/start` and `/whoami` are intentionally left out of the bot's `/`-menu (only `/play`
and `/stop` show there) but still work when typed.

## Motion detection announcements

If `ONVIF_USER` and `ONVIF_PASS` are set, the bot watches the configured ONVIF
camera for motion. Generic motion (wind, shadows, etc.) is ignored — an
announcement only happens when the camera's object classification identifies a
person, animal, or vehicle, and the announcement names which one it is. Motion
events are debounced (one announcement per 30s). Once the announcement finishes,
the interrupted track resumes from approximately where it was interrupted (within
a few seconds, not frame-exact).

## Known limitation

Live YouTube streams (e.g. 24/7 lofi radio streams) resolve to an HLS manifest whose
content type `yt-dlp`/`catt` can't always detect, which the Nest Mini fails to play.
Regular (non-live) videos work reliably.
