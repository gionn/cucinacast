# CucinaCast

Telegram bot that searches YouTube and casts the top result to a Nest Mini (Chromecast).
It can also speak a custom announcement on demand, with an ONVIF camera configured,
announce motion (person/animal/vehicle) automatically, and, with a Bluetooth adapter,
track who's home via their phones' presence.

Named after "cucina" (Italian for kitchen) — the Nest Mini it talks to lives in the
kitchen.

## Quickstart

```
git clone <this repo> && cd cucinacast
./setup.sh
```

Fill in the scaffolded `.env` (at minimum `TELEGRAM_BOT_TOKEN` and `OWNER_USER_ID`,
see [Setup](#setup) below), then:

```
sudo systemctl start cucinacast
sudo journalctl -u cucinacast -f
```

Message the bot on Telegram to confirm it replies, then send it a song name to
play it on the Nest Mini.

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
- `QUIET_HOURS_START` / `QUIET_HOURS_END` — local-time hours (0-23) defining a window
  in which motion-triggered announcements are suppressed (default `22`/`8`, i.e.
  10pm-8am). Only affects automatic doorbell announcements from motion detection;
  the manual `/announce` command always works.

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

## Development

```
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest
```

`requirements-dev.txt` pulls in `requirements.txt` plus `pytest`. The test suite
covers the search-ranking/caching logic in `castyt.py`, the play-history storage
logic in `storage.py`, the Bluetooth-presence storage logic in
`storage_bluetooth.py`, and the presence state machine in `presence.py`, with the
actual `yt-dlp`/sqlite calls mocked/isolated and the `hcitool`/`bluetoothctl`
subprocesses never invoked. Everything that touches the real Chromecast, camera,
Bluetooth hardware, or Telegram API has no automated coverage — verify those
manually against the real Nest Mini.

## Bot commands

- `/start` — explains what the bot does and lists the commands below.
- `/play <query>` — search YouTube and play the top result on the Nest Mini. Also
  triggered by sending any plain text message (no need for the `/play` prefix). Queues
  the top search results and auto-advances to the next one when the current track
  finishes; when the queue runs out it fetches more results for the same query and
  keeps going, indefinitely, until `/stop` is sent. Unplayable results (private/removed
  videos) are skipped automatically.
- `/announce <text>` — speak a custom message on the Nest Mini, interrupting current
  playback, then resume it from approximately where it left off. Same two-step prompt
  as `/play` if sent with no text.
- `/stop` — stop playback and clear the queue.
- `/athome` — list registered Bluetooth devices and whether each is home or away
  (with the last time each was seen).
- `/adddevice` — register a Bluetooth device to track. Prompts for a nickname, runs
  a 15-second discovery scan, then pairs (confirming any passkey on your phone) and
  trusts the device. The device can be removed again with `/rmdevice`.
- `/rmdevice <nickname|mac>` — stop tracking a device, by nickname or MAC address.
- `/whoami` — reply with your Telegram user id, to put in `OWNER_USER_ID` /
  `ALLOWED_USER_IDS`.

`/start` and `/whoami` are intentionally left out of the bot's `/`-menu (only the
casting and presence commands show there) but still work when typed.

## Motion detection announcements

If `ONVIF_USER` and `ONVIF_PASS` are set, the bot watches the configured ONVIF
camera for motion. Generic motion (wind, shadows, etc.) is ignored — an
announcement only happens when the camera's object classification identifies a
person, animal, or vehicle, and the announcement names which one it is. Motion
events are debounced (one announcement per 30s). Once the announcement finishes,
the interrupted track resumes from approximately where it was interrupted (within
a few seconds, not frame-exact). Announcements are skipped entirely during quiet
hours (`QUIET_HOURS_START`/`QUIET_HOURS_END`, default 10pm-8am local time).

If `ffmpeg` is available on `PATH`, a short clip of the camera's live sub-stream is
also sent to the bot owner on Telegram alongside the spoken announcement. If
`ffmpeg` is missing, this is skipped and only the audio announcement plays — motion
detection itself is unaffected either way.

## Bluetooth presence

If the machine running the bot has a Bluetooth adapter, the bot can track who's home
by monitoring registered phones. This needs `hcitool` and `bluetoothctl` installed and
the service user in the `bluetooth` group (`sudo usermod -aG bluetooth <user>`); if
your machine lacks a working adapter (or the group isn't set up), the feature is
disabled with a warning at startup and `/athome`/`/adddevice`/`/rmdevice` report that
no devices are registered.

Register a device with `/adddevice` — give it a nickname, then pick your phone from
the 15-second discovery scan. The bot pairs (you confirm any passkey shown on the
phone) and trusts the device. Pairing is only needed once, at registration; afterwards
the phone's Bluetooth can stay on with it never being discoverable.

Every 10 minutes the bot pages each registered device via `hcitool name <mac>`, which
works for any powered-on phone whether or not it's paired or discoverable. A device
flips to "away" only after 3 consecutive missed polls (a single probe failure can't
flap the state), and the miss count survives bot restarts. `/athome` shows each
nickname with home/away status and last-seen time, and the owner gets a Telegram
message whenever someone arrives or leaves.

The only real limitation: if a phone's Bluetooth is fully toggled off, the radio is
silent and the bot will read it as "away" (after the 3-strike grace period). That's a
property of Bluetooth itself, not of the bot.

## Known limitation

Live YouTube streams (e.g. 24/7 lofi radio streams) resolve to an HLS manifest whose
content type `yt-dlp`/`catt` can't always detect, which the Nest Mini fails to play.
Regular (non-live) videos work reliably.
