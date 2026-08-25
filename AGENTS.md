## What this is

CucinaCast: a Telegram bot that searches YouTube and casts the result to a Nest Mini
(Chromecast) speaker on the local network. `cast_test.py` is a standalone CLI for
exercising the same casting logic without Telegram.

## Commands

```
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# standalone cast test
NEST_DEVICE_NAME="Cucinino" ./.venv/bin/python cast_test.py "some song name"

# run the bot (reads .env via python-dotenv)
./.venv/bin/python bot.py

# syntax-check after edits (no test suite exists)
./.venv/bin/python -m py_compile bot.py castyt.py cast_test.py

# install/refresh as a systemd service (creates .venv, installs deps every run)
./setup.sh
```

Required env vars (see README.md): `TELEGRAM_BOT_TOKEN`, `OWNER_USER_ID`. Optional:
`NEST_DEVICE_NAME`, `ALLOWED_USER_IDS`.

There is no test suite. Verification is manual: run `cast_test.py` or the bot against
the real Nest Mini and confirm audio actually plays.

## Architecture

- `castyt.py` — all casting/search logic, no Telegram dependency.
  - `search_youtube(query, count)` uses `yt-dlp`'s `ytsearchN:` pseudo-URL (no API key).
  - `Player` is a singleton (`player = Player()`) that holds one persistent
    `catt.api.CattDevice` connection and a search-result queue. It registers itself as
    a `pychromecast` media status listener (`new_media_status`) to detect when a track
    finishes (`player_state == "IDLE"` and `idle_reason == "FINISHED"`) and
    auto-advances to the next queued result. When the queue runs out it re-searches the
    same query for more results (`_grow_queue_locked`), deduping by video id, so
    playback continues indefinitely until `/stop`. Unplayable entries (private/removed
    videos) are skipped automatically (`_try_play_locked`).
  - All device/queue mutation goes through `self._lock` since the pychromecast status
    callback fires on its own socket thread, concurrently with bot-triggered calls.
  - Casting a URL requires building a `catt.stream_info.StreamInfo` with
    `cast_info=device._cast.cast_info` so format selection knows the Nest Mini is
    audio-only — without it, `yt-dlp` picks a video format the device can't play.
    Similarly, `content_type` must be passed through explicitly
    (`stream.guessed_content_type`); catt's `play_media_url` defaults to `video/mp4`
    which breaks audio-only/live streams.
  - Live YouTube streams resolve to an HLS manifest whose content type isn't always
    detected — this is a known limitation, not something to "fix" reactively.
  - Device discovery (`_get_device`) retries a few times since local mDNS discovery is
    occasionally flaky.
- `bot.py` — python-telegram-bot wiring only; delegates all real work to
  `castyt.player`. Handlers (`play`, `stop`) call into `Player` via
  `asyncio.to_thread(...)` — this is required, not just a style choice: pychromecast's
  discovery does blocking zeroconf I/O, which conflicts with running inside
  python-telegram-bot's asyncio event loop if called directly on that thread.
  - Access control: `OWNER_USER_ID` (required) is always implicitly allowed and gets
    notified of unauthorized attempts; `ALLOWED_USER_IDS` (optional, comma-separated)
    additionally allow-lists other users. If `ALLOWED_USER_IDS` is empty, the bot is
    open to everyone.
