# CucinaCast

Telegram bot that searches YouTube and casts the result to a Nest Mini
(Chromecast) on the LAN. It also speaks gTTS announcements on demand and, when
an ONVIF camera is configured, plays a sound effect on classified motion.

`README.md` is the user-facing doc (setup, commands, env vars): update it when a
change affects how people run or use the bot. This file holds internal notes.

In a new worktree, create the venv and install `requirements-dev.txt` before
committing: the pre-commit hook runs the whole suite whenever a `.py` file is
staged, and a missing venv fails the commit.

## Commands

```
python3 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt

./.venv/bin/python bot.py               # reads .env via python-dotenv
./.venv/bin/python -m pytest            # unit suite, no speaker/network needed
pre-commit run --all-files              # lint, format and pytest; same as CI
./.venv/bin/python tts.py "some text"   # standalone TTS, writes an MP3, no speaker
sudo ./setup.sh                         # venv, deps, .env scaffold, systemd unit
```

`ruff` lives only in pre-commit's environment, so `ruff check`/`ruff format` are
not on `PATH`; the hooks auto-fix, and whatever they rewrite has to be re-staged.
`requirements-dev.txt` is `requirements.txt` plus pytest; the devcontainer
installs pre-commit on top, and `setup.sh`/the systemd service install only
`requirements.txt`.

Only `TELEGRAM_BOT_TOKEN` and `OWNER_USER_ID` are required; everything else is
optional and documented in README. With `NEST_DEVICE_NAME` unset, `_get_device`
discovers the first Chromecast by friendly name (`_discover_cast`) instead of
requiring a configured default device.

## Tests

`tests/` has mocked unit tests covering `castyt`, `storage`, `bot`, `motion`,
`announce`, `tts`, `phrases` and `sounds`; no hardware or network is involved.
`tests/conftest.py` repoints `storage.DB_PATH` at a per-test temp directory and
sets fake Telegram env vars, so tests never touch the real `cucinacast.db`.

`tests/test_integration_device.py` is the only test that drives the real speaker.
It skips unless `CUCINACAST_INTEGRATION=1`, and only then loads `.env`. It needs
the speaker reachable on the LAN, yt-dlp/YouTube, gTTS, and the announce port
reachable from the device. Stop the bot service first: both overwrite
`tts.DEFAULT_PATH`, so an announcement can race a live one.

```
# quick: search, play, stop a few seconds later (~10s)
CUCINACAST_INTEGRATION=1 .venv/bin/python -m pytest tests/test_integration_device.py -k stop

# both, including resume-after-announcement and auto-advance (waits for a track to end)
CUCINACAST_INTEGRATION=1 .venv/bin/python -m pytest tests/test_integration_device.py -s
```

Knobs are `CUCINACAST_IT_PLAY_QUERY` (quick test) and `CUCINACAST_IT_QUERY` (full
test), plus `CUCINACAST_IT_{START,STATUS,FINISH}_TIMEOUT`. The search pool is
ranked by views and then shuffled, so a run plays a random entry from the top 20
rather than a stable "top result"; if that entry ends before the 10s wait, the
queue advances on its own and the full test's index assertions fail.

## Architecture

- `castyt.py` — search and casting, no Telegram dependency.
  - `search_youtube` fetches `max(count, SEARCH_POOL_SIZE=20) + len(exclude_ids)`
    results through yt-dlp's `ytsearchN:` pseudo-URL (no API key), caches the raw
    pool per query, drops excluded ids, ranks the rest by `view_count`, keeps
    `count` and shuffles only that slice. A cached pool is reused only while it
    still yields `count` candidates after exclusions; otherwise it is re-fetched
    and replaces the cache. Callers pass recently played ids
    (`storage.recent_ids`) plus ids already queued as `exclude_ids`.
  - `player` is a module-level singleton holding one `CattDevice` and the queue.
    Every device or queue mutation takes `self._lock`, because `new_media_status`
    runs on pychromecast's socket thread.
  - `new_media_status` advances only on `IDLE` + `FINISHED`, and only for a
    session the bot started: the media session id is authoritative, the content
    id a fallback. Media the user started on the speaker is never taken over, and
    `_grow_queue_locked` re-searches the current query (refusing to run without
    one) when the queue empties.
  - Session ids are captured from a later status update that shows our content as
    current, not right after `play_media_url`: catt's `block_until_active` can
    return on the previous session, and a stale id never matches our own FINISHED.
  - Announce-resume state lives on `Player` (`_announcing`, `_interrupted_at_seconds`)
    and is cleared by `stop()` and by a fresh play. The position is approximate:
    wall-clock elapsed time, captured before speaking, passed back as
    `current_time`. The resume re-resolves the stream, so the content id changes.
    If the resume fails, it falls back to `_try_play_locked` instead of stopping.
  - `StreamInfo` must be built with `cast_info=device._cast.cast_info` and played
    with `content_type=stream.guessed_content_type`; otherwise yt-dlp/catt choose
    a video/mp4 format the audio-only device cannot play. Live YouTube streams
    resolve to HLS whose content type is often undetected; known limitation.
  - `_get_device` retries discovery and raises `DeviceUnavailable`, which aborts
    the attempt instead of skipping through the queue; a `CastError` skips only
    that entry. Each connection gets a persistent zeroconf instance injected into
    its socket client: catt stops the discovery browser's shared one, and without
    the replacement pychromecast's reconnect thread spins on
    `AssertionError: Zeroconf instance loop must be running`.
- `bot.py` — Telegram wiring only. Handler bodies call into `player`,
  `synthesize_and_serve` and `serve_sound` through `asyncio.to_thread`, because
  catt/zeroconf discovery blocks and would stall the event loop.
  - Bare `/play` and `/announce` fall back to a two-step prompt: the user id goes
    into `_awaiting_play_text`/`_awaiting_announce_text`, and `_route_text` (the
    catch-all text handler) consumes the next message before defaulting to `/play`.
  - Access: `OWNER_USER_ID` is always allowed and is DMed about denied attempts;
    an empty `ALLOWED_USER_IDS` opens the bot to everyone.
  - `post_init` starts `motion.run_forever` with plain `asyncio.create_task` (it
    runs before `Application.start()`, where `app.create_task` is not tracked) and
    `post_stop` cancels it, both only when motion detection is enabled.
- `motion.py` — ONVIF pullpoint events, no Telegram/TTS/casting dependency.
  - Motion and object classification arrive as separate events, in either order.
    A motion event creates a future that `_evaluate_motion` waits on for
    `MOTION_CLASSIFICATION_TIMEOUT_SECONDS` (default 10), seeded with a
    classification seen just before the alarm, so late and early classifications
    both count. `pending_tasks` allows one evaluation at a time.
  - Debounce starts only once a category resolves, so unclassified motion (wind,
    shadows) cannot suppress a real alert. `run_forever` retries after 30s, and
    `watch_motion`'s `finally` cancels pending evaluations and tears down the
    subscription.
  - `describe_object` returns a language-neutral category
    ("person"/"animal"/"vehicle"/"unknown"), never wording.
- `announce.py` — a Chromecast can only fetch HTTP(S), so audio is served by a
  stdlib `ThreadingHTTPServer` on `ANNOUNCE_PORT` (default 8765). One fixed file
  (`tts.DEFAULT_PATH`) is overwritten per announcement and the handler 404s
  everything else; back-to-back announcements can race, which is accepted.
  `ANNOUNCE_HOST` overrides LAN IP detection for multi-NIC hosts.
- `tts.py`, `sounds.py`, `phrases.py` — synthesis, category→asset lookup, and
  TTS language plus quiet-hour gating. Each stays separate so `motion.py` stays
  language-neutral and serving stays free of asset knowledge.
- `storage.py` — sqlite (`cucinacast.db`, gitignored). `plays` keeps the last 20
  plays per query for recency exclusion; `search_cache` holds the raw pool with a
  7-day TTL. Schema and legacy cleanup live in `init_db()`, called once from
  `bot.main()`, so `_connect()` stays free of DDL. Query lookups use `IS ?` so a
  `NULL` query compares correctly.
