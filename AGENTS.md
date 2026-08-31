## What this is

CucinaCast: a Telegram bot that searches YouTube and casts the result to a Nest Mini
(Chromecast) speaker on the local network.

When a change adds or changes a user-facing feature (a bot command, an env var
affecting behavior, etc.), update `README.md` to match — it's the user-facing
doc, kept separate from this file's internal architecture notes.

When working in a new worktree for this repo, create the venv and install
`requirements-dev.txt` right away (see Commands below) so `pytest` is ready
before committing — the pre-commit hook runs the test suite, and a missing
venv fails it.

## Commands

```
python3 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt

# run the bot (reads .env via python-dotenv)
./.venv/bin/python bot.py

# standalone TTS test (writes an MP3, no camera/chromecast needed — useful to
# isolate whether a problem is in synthesis or in casting/playback)
./.venv/bin/python tts.py "some announcement text"

# syntax-check after edits
./.venv/bin/python -m py_compile bot.py castyt.py motion.py announce.py tts.py phrases.py storage.py storage_bluetooth.py presence.py

# run the test suite (unit tests for search ranking/caching, play-history and
# bluetooth-presence storage logic, and the presence state machine; everything
# else is still verified manually, see below)
./.venv/bin/python -m pytest

# install/refresh as a systemd service (creates .venv, installs deps every run)
./setup.sh
```

`requirements-dev.txt` pulls in `requirements.txt` plus `pytest`; `setup.sh` and the
systemd service only need `requirements.txt`.

Required env vars (see README.md): `TELEGRAM_BOT_TOKEN`, `OWNER_USER_ID`. Optional:
`NEST_DEVICE_NAME`, `ALLOWED_USER_IDS`, `ONVIF_USER`, `ONVIF_PASS`, `ONVIF_HOST`,
`ONVIF_PORT`, `ANNOUNCE_PORT`, `ANNOUNCE_HOST`, `TTS_LANG`, `LOG_LEVEL`,
`HTTPX_LOG_LEVEL`, and the Bluetooth-presence tuning vars `BT_POLL_INTERVAL_SECONDS`,
`BT_MISS_THRESHOLD`, `BT_PROBE_TIMEOUT_SECONDS`, `BT_PROBE_ATTEMPTS`,
`BT_PROBE_RETRY_DELAY_SECONDS`, `BT_DISCOVERY_TIMEOUT_SECONDS`, `BT_PAIR_TIMEOUT_SECONDS`,
`BT_PASSKEY_CONFIRM_TIMEOUT_SECONDS`.

`pytest` covers the search-ranking/caching logic in `castyt.py`, the storage
logic in `storage.py`/`storage_bluetooth.py`, and the presence state machine in
`presence.py` (`tests/`), with the actual `yt-dlp`/sqlite calls mocked/isolated
and the `hcitool`/`bluetoothctl` subprocesses never invoked. Everything that
touches the real Chromecast, camera, Bluetooth hardware, or Telegram API still
has no automated coverage — verify those manually by running the bot against
the real Nest Mini and confirming audio actually plays.

## Architecture

- `castyt.py` — all casting/search logic, no Telegram dependency.
  - `search_youtube(query, count, exclude_ids)` fetches a pool of at least
    `max(count, SEARCH_POOL_SIZE) + len(exclude_ids)` results via `yt-dlp`'s
    `ytsearchN:` pseudo-URL (no API key; `_fetch_live`) — the `exclude_ids` term
    guarantees at least `count` survivors even in the worst case where every
    excluded id happens to fall inside the fetched pool. It drops any id in
    `exclude_ids`, ranks the rest by `view_count` descending, keeps the top
    `count`, and shuffles just that slice — so playback favors popular tracks
    without always playing them in the same order. The raw pool is cached in
    `storage.py` per query (`storage.cached_pool`/`store_pool`); a repeat search
    serves from cache only if it's fresh and, after excluding ids, still leaves
    at least `count` candidates — otherwise it falls back to a live fetch (sized
    to account for the current `exclude_ids`) and re-caches the result.
  - `Player` is a singleton (`player = Player()`) that holds one persistent
    `catt.api.CattDevice` connection and a search-result queue. It registers itself as
    a `pychromecast` media status listener (`new_media_status`) to detect when a track
    finishes (`player_state == "IDLE"` and `idle_reason == "FINISHED"`) and
    auto-advances to the next queued result. When the queue runs out it re-searches the
    same query for more results (`_grow_queue_locked`), excluding both video ids
    already in the queue and ids from `storage.recent_ids` (recently played for that
    query), so playback continues indefinitely until `/stop` without repeating
    recent picks. Unplayable entries (private/removed videos) are skipped
    automatically (`_try_play_locked`), which also records a successful play via
    `storage.record_play` — announcement resumes (in `new_media_status`) don't call
    `_try_play_locked`, so they aren't recorded as a new play.
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
  - Each device connection gets a persistent zeroconf instance injected onto its
    socket client (`Player._live_zconf`, created once and never closed). catt
    hands every Chromecast the discovery browser's shared zeroconf and then
    stops it, so without this pychromecast's background reconnect thread would
    resolve services against a dead loop and spin on
    `AssertionError: Zeroconf instance loop must be running` forever.
- `bot.py` — python-telegram-bot wiring only; delegates all real work to
  `castyt.player`. Handlers (`play`, `announce`, `stop`) call into `Player` via
  `asyncio.to_thread(...)` — this is required, not just a style choice: pychromecast's
  discovery does blocking zeroconf I/O, which conflicts with running inside
  python-telegram-bot's asyncio event loop if called directly on that thread.
  - `/play` and `/announce` both need a text argument that Telegram has no way to
    require up front — tapping either from the command menu always fires the bare
    command with no args. Each command handler falls back to a two-step prompt: if
    called with no args, it records the user id in a pending-state set
    (`_awaiting_play_text`/`_awaiting_announce_text`) and asks for the missing text;
    the next plain-text message from that user is consumed by `_route_text` (the
    catch-all `MessageHandler`), which checks the pending sets before falling back to
    its default of treating the text as a `/play` query — this keeps "just send a
    plain text message to search" working unchanged for anyone who never invoked
    `/announce`/bare `/play` in the first place.
  - Access control: `OWNER_USER_ID` (required) is always implicitly allowed and gets
    notified of unauthorized attempts; `ALLOWED_USER_IDS` (optional, comma-separated)
    additionally allow-lists other users. If `ALLOWED_USER_IDS` is empty, the bot is
    open to everyone.
  - `post_init` starts `motion.run_forever(_on_motion)` as a background task via
    plain `asyncio.create_task`, not `app.create_task` — `post_init` runs before
    `Application.start()`, when `app.create_task` would warn and not track the
    task for `stop()` to await. The task is kept in a module-level `_motion_task`
    and cancelled/awaited from a `post_stop` hook instead, only if
    `motion.motion_detection_enabled()` is true — bots without a camera
    configured are unaffected. `_on_motion` skips unclassified
    ("unknown"-category) motion entirely — generic motion is usually uninteresting
    (wind, shadows, etc.), so only motion the camera actually classified (person/
    animal/vehicle) gets announced. It also skips announcing during quiet hours
    (`phrases.in_quiet_hours()`) — this check only gates the automatic motion path,
    not the manual `/announce` command. Otherwise it looks up the wording via
    `phrases.announcement_text` and casts it via `announce.synthesize_and_serve` +
    `player.announce` (both via `asyncio.to_thread`, same pattern as `play`/`stop`).
- `motion.py` — ONVIF motion-detection logic, no Telegram/TTS/casting dependency
  (mirrors `castyt.py`'s separation).
  - `watch_motion(on_motion)` subscribes to the camera's pullpoint events (same
    `create_pullpoint_manager`/`PullMessages` flow as the retired PoC).
    `discover_camera()` runs via `asyncio.to_thread` since it's blocking
    WS-Discovery I/O, same reason as `castyt.py`'s Chromecast discovery.
  - Classification for an object can arrive after its motion event, not
    before. A confirmed motion event schedules `_announce_after_delay`
    (tracked in `pending_tasks`, both to avoid premature GC and to gate
    classification recording/overlapping evaluations to one at a time), which
    waits `CLASSIFICATION_WAIT_SECONDS` before reading `last_object_class` and
    invoking `on_motion`.
  - The debounce cooldown (`DEBOUNCE_SECONDS`) only starts once a recognized
    category is resolved, not the moment raw motion fires — otherwise an
    unclassified event (wind, shadows) would suppress a real one for 30s.
  - `run_forever` wraps `watch_motion` in a retry loop so a transient camera or
    network failure can't crash the bot process.
  - `watch_motion`'s `finally` cancels and awaits any still-pending
    `_announce_after_delay` task before shutting down the subscription — those
    tasks are independent children of the loop, not of `watch_motion`, so
    without this an in-flight one could still fire an announcement after a
    subscription failure or shutdown has already moved on to a new watcher.
  - `motion_detection_enabled()` gates the whole feature on `ONVIF_USER`/
    `ONVIF_PASS` being set — both are optional at the bot level.
  - `describe_object` returns a language-neutral category
    ("person"/"animal"/"vehicle"/"unknown"), not wording — localization into an
    actual sentence is `phrases.py`'s job, keeping `motion.py` free of any
    TTS/language concern.
- `presence.py` — Bluetooth presence tracking (who's home), no Telegram/TTS/
  casting dependency (mirrors `motion.py`'s separation). Gated on a working
  adapter via `bluetooth_available()` (`hcitool dev` seeing `hci0`), which logs
  a startup warning with setup advice if the adapter can't be queried.
  - Device lookup uses `hcitool name <mac>` (paging), which works for any
    powered-on phone regardless of pairing/discoverability — unlike a discovery
    scan, which only sees discoverable devices. `hcitool`/`bluetoothctl` run
    unprivileged: the HCI socket is accessible once the user is in the
    `bluetooth` group, and `bluetoothctl` talks over the BlueZ DBus API.
  - `run_forever(on_transition)` polls every `POLL_INTERVAL_SECONDS` (10 min)
    via `check_presence`, which probes each registered device through
    `asyncio.to_thread` (blocking subprocess I/O) and applies the 3-strike
    rule: a device flips home→away only after `MISS_THRESHOLD` consecutive
    misses, so a single probe failure (bluetooth hiccup) can't flap the state.
    Miss counts persist through `storage_bluetooth`, so a bot restart doesn't
    reset the countdown. Transitions (home/away flips) are collapsed to at most
    one per nickname per poll (`_collapse_transitions`, preferring home when a
    nickname has devices on both sides) and go to `on_transition(transition)`,
    which `bot.py` uses to notify the owner on Telegram. Restarts on failure
    like `motion.run_forever`.
  - `discover_devices()` runs a temporary `bluetoothctl scan on` and parses the
    `[NEW] Device` lines; `pair_device(mac, on_prompt)` drives an interactive
    `bluetoothctl` session (agent on / default-agent / pair / trust) and calls
    `on_prompt(passkey)` when bluetoothctl asks to confirm a passkey, so
    `bot.py` can relay the confirmation to the owner on Telegram and feed the
    reply back. Returns success/failure — pairing is only ever needed during
    `/adddevice`, never for presence probing.
- `phrases.py` — localized wording for doorbell announcements. `TTS_LANG` (env,
  default `en`) selects the language; `announcement_text(category)` maps a
  `motion.describe_object` category to a sentence in that language, falling back
  to English wording for unconfigured `TTS_LANG` values. Kept separate from
  `bot.py` (wiring only) and from `motion.py`/`tts.py` (language-agnostic) so
  adding a language/wording is a one-file change. `in_quiet_hours(now=None)` checks
  the current local hour against `QUIET_HOURS_START`/`QUIET_HOURS_END` (default
  `22`/`8`), handling the overnight wraparound; `bot.py`'s `_on_motion` uses it to
  suppress motion-triggered announcements at night.
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
    are already debounced and only one announcement is ever in flight. The
    user-triggered `/announce` command isn't debounced, so back-to-back
    announcements (or one racing a motion event) can overwrite the file before
    the Nest Mini finishes fetching the prior one — accepted as a known
    limitation rather than adding per-announcement filenames/a queue.
  - `_AnnounceHandler` only serves that one fixed path (404s everything else) —
    deliberately not `SimpleHTTPRequestHandler` over the whole temp directory,
    which would expose unrelated temp files on a shared machine.
  - `_get_lan_ip()` detects the host's outbound LAN IP (not `localhost`, which the
    Chromecast can't resolve) via a connect-less UDP socket trick; override with
    `ANNOUNCE_HOST` for multi-NIC machines or networks without external
    connectivity (where the detection trick itself would fail).
- `storage.py` — sqlite persistence (`cucinacast.db`, gitignored) for both
  play history and the search-result cache, no Telegram/casting dependency.
  - `plays` table backs `record_play`/`recent_ids`: each query keeps only its
    `HISTORY_LIMIT` most recent plays (older rows for that query are deleted on
    each `record_play`), so recency-based exclusion needs no unbounded memory or
    disk growth.
  - `search_cache` table backs `cached_pool`/`store_pool`: `cached_pool` returns
    `None` (a cache miss) if the query has fewer rows than the caller's requested
    `min_count`, or if the cached rows are older than `CACHE_TTL_SECONDS` — either
    triggers a live re-fetch in `castyt.py`, which then calls `store_pool` to
    fully replace that query's cached rows.
  - Each function opens and closes its own short-lived `sqlite3.connect`; no
    long-held connection or extra locking, since `Player` already serializes its
    own calls into this module through `self._lock`.
- `storage_bluetooth.py` — the `devices` table (registered MACs, nicknames, and
  the persistent home/away + miss-count state) in its own module, same
  short-lived-connection pattern as `storage.py`. `presence.py` is the only
  consumer. Kept separate so the play-history/cache module doesn't accumulate
  unrelated concerns.
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
  - The resume itself goes through the same failure handling as normal queue
    advancement: if `_play_current_locked` raises, `new_media_status` falls
    back to `_try_play_locked` (resetting the cast device on `CastError` and
    advancing the index first) instead of leaving playback stopped on a
    transient cast failure.
