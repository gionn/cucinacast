## What this is

CucinaCast: a Telegram bot that searches YouTube and casts the result to a Nest Mini
(Chromecast) speaker on the local network.

## Commands

```
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# run the bot (reads .env via python-dotenv)
./.venv/bin/python bot.py

# standalone TTS test (writes an MP3, no camera/chromecast needed — useful to
# isolate whether a problem is in synthesis or in casting/playback)
./.venv/bin/python tts.py "some announcement text"

# syntax-check after edits (no test suite exists)
./.venv/bin/python -m py_compile bot.py castyt.py motion.py announce.py tts.py phrases.py

# install/refresh as a systemd service (creates .venv, installs deps every run)
./setup.sh
```

Required env vars (see README.md): `TELEGRAM_BOT_TOKEN`, `OWNER_USER_ID`. Optional:
`NEST_DEVICE_NAME`, `ALLOWED_USER_IDS`, `ONVIF_USER`, `ONVIF_PASS`, `ONVIF_HOST`,
`ONVIF_PORT`, `ANNOUNCE_PORT`, `ANNOUNCE_HOST`, `TTS_LANG`, `LOG_LEVEL`,
`HTTPX_LOG_LEVEL`.

There is no test suite. Verification is manual: run the bot against the real Nest
Mini and confirm audio actually plays.

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
  - `post_init` starts `motion.run_forever(_on_motion)` as a background PTB task
    (`app.create_task`, so it's tracked/cancelled on shutdown and exceptions get
    logged) only if `motion.motion_detection_enabled()` is true — bots without a
    camera configured are unaffected. `_on_motion` skips unclassified
    ("unknown"-category) motion entirely — generic motion is usually uninteresting
    (wind, shadows, etc.), so only motion the camera actually classified (person/
    animal/vehicle) gets announced. Otherwise it looks up the wording via
    `phrases.announcement_text` and casts it via `announce.synthesize_and_serve` +
    `player.announce` (both via `asyncio.to_thread`, same pattern as `play`/`stop`).
- `motion.py` — ONVIF motion-detection logic, no Telegram/TTS/casting dependency
  (mirrors `castyt.py`'s separation).
  - `watch_motion(on_motion)` subscribes to the camera's pullpoint events (same
    `create_pullpoint_manager`/`PullMessages` flow as the retired PoC) and
    debounces motion (`DEBOUNCE_SECONDS`).
  - On some cameras the classification event for an object arrives *after* the
    motion event it belongs to, not before. A confirmed motion event schedules
    `_announce_after_delay` (an `asyncio.create_task`, tracked in `pending_tasks`
    to avoid premature GC) which waits `CLASSIFICATION_WAIT_SECONDS` before
    reading `last_object_class` and invoking `on_motion` — this lets a
    same-batch classification event that follows the motion event still be
    picked up. Classification events are only recorded into `last_object_class`
    while that window is open (`time.monotonic() - last_announced <
    CLASSIFICATION_WAIT_SECONDS`); a classification arriving after the window
    closed is discarded rather than leaking into the *next*, unrelated motion
    event's announcement.
  - `on_motion` is awaited directly from `_announce_after_delay` — no extra
    thread plumbing needed since `watch_motion` is already a coroutine.
  - `run_forever` wraps `watch_motion` in a retry loop so a transient camera or
    network failure can't crash the bot process.
  - `motion_detection_enabled()` gates the whole feature on `ONVIF_USER`/
    `ONVIF_PASS` being set — both are optional at the bot level.
  - `describe_object` returns a language-neutral category
    ("person"/"animal"/"vehicle"/"unknown"), not wording — localization into an
    actual sentence is `phrases.py`'s job, keeping `motion.py` free of any
    TTS/language concern.
- `phrases.py` — localized wording for doorbell announcements. `TTS_LANG` (env,
  default `en`) selects the language; `announcement_text(category)` maps a
  `motion.describe_object` category to a sentence in that language, falling back
  to English wording for unconfigured `TTS_LANG` values. Kept separate from
  `bot.py` (wiring only) and from `motion.py`/`tts.py` (language-agnostic) so
  adding a language/wording is a one-file change.
- `tts.py` — TTS synthesis only (via `gTTS`, chosen since the project already
  requires internet for YouTube), no serving/casting dependency. `synthesize(text,
  lang, path)` saves an MP3 and returns its path. Kept separate from `announce.py`
  so other features can reuse synthesis without the HTTP-serving concern, and so
  it can be exercised standalone (`python tts.py "some text"`) to check whether a
  problem is in the TTS output itself vs. in casting/playback.
- `announce.py` — imports `tts.synthesize` and serves the resulting MP3 over a
  small stdlib `ThreadingHTTPServer` on the LAN, since the Chromecast can only
  play HTTP(S) URLs, not local file paths.
  - The audio file is a single fixed path (`tts.DEFAULT_PATH`), overwritten per
    announcement — no per-announcement filenames or cleanup, since motion events
    are already debounced and only one announcement is ever in flight.
  - `_AnnounceHandler` only serves that one fixed path (404s everything else) —
    deliberately not `SimpleHTTPRequestHandler` over the whole temp directory,
    which would expose unrelated temp files on a shared machine.
  - `_get_lan_ip()` detects the host's outbound LAN IP (not `localhost`, which the
    Chromecast can't resolve) via a connect-less UDP socket trick; override with
    `ANNOUNCE_HOST` for multi-NIC machines or networks without external
    connectivity (where the detection trick itself would fail).
- `Player.announce(url)` in `castyt.py` interrupts current playback to play an
  arbitrary announcement URL, tracked via an `_announcing` flag on `Player`. The
  `new_media_status` FINISHED callback branches on this flag: after an
  announcement it resumes the current queue entry near where it was interrupted;
  otherwise it advances the queue as before. `stop()` also clears `_announcing`
  so a `/stop` mid-announcement can't leave a stale flag.
  - Resume position is approximated, not exact: `_play_current_locked` records
    `_track_started_at = time.monotonic() - (current_time or 0)` whenever it
    (re)starts a track. `announce()` captures
    `_interrupted_at_seconds = time.monotonic() - _track_started_at` *before*
    playing the announcement (not after — the announcement's own duration
    would otherwise inflate the resumed position by however long it took to
    speak), and that saved value is passed as `current_time` to
    `play_media_url` (a native `catt`/pychromecast parameter) on resume. This
    drifts by a few seconds (network/buffering delay isn't accounted for) —
    accepted, since exact position would require reading the device's own
    playback clock.
