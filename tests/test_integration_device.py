"""End-to-end playback checks against a real Nest Mini.

Opt in with CUCINACAST_INTEGRATION=1 so the normal suite and CI never need a
speaker. Needs LAN access to the Chromecast, YouTube (yt-dlp) and Google TTS,
and the announce HTTP port reachable from the device:

    CUCINACAST_INTEGRATION=1 ./.venv/bin/python -m pytest tests/test_integration_device.py -s

Two tests live here. test_search_and_stop_playback is the quick smoke test: it
plays a search result and stops PLAY_SECONDS later. test_play_announce_resume_
then_advance_to_next_track is the full run, and it lasts as long as the track it
picks, since auto-advance only happens when that track ends. Run just the quick
one with `-k stop`.
"""

import logging
import os
import time

import pytest
from dotenv import load_dotenv

import announce
import castyt
import tts

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.integration

if os.environ.get("CUCINACAST_INTEGRATION") != "1":
    pytest.skip(
        "set CUCINACAST_INTEGRATION=1 to run against a real Nest Mini",
        allow_module_level=True,
    )

# The bot reads its config from .env; only load it for an opted-in run so a
# plain suite never inherits NEST_DEVICE_NAME and friends.
load_dotenv()

QUERY = os.environ.get("CUCINACAST_IT_QUERY", "nokia 3310 classic ringtone")
PLAY_QUERY = os.environ.get("CUCINACAST_IT_PLAY_QUERY", "nokia tune")
PLAY_SECONDS = 5
ANNOUNCEMENT_TEXT = (
    "This is the CucinaCast integration test announcement. Playback should resume in a moment."
)
START_TIMEOUT_SECONDS = float(os.environ.get("CUCINACAST_IT_START_TIMEOUT", "60"))
STATUS_TIMEOUT_SECONDS = float(os.environ.get("CUCINACAST_IT_STATUS_TIMEOUT", "60"))
FINISH_TIMEOUT_SECONDS = float(os.environ.get("CUCINACAST_IT_FINISH_TIMEOUT", "600"))
POLL_INTERVAL_SECONDS = 0.5
INTERRUPT_AFTER_SECONDS = 10
# Resume is approximated from wall-clock elapsed time, so it drifts by the
# buffering delay between issuing the load and the receiver actually playing.
RESUME_DRIFT_ALLOWANCE_SECONDS = 15


def _device_status(player):
    return player._ensure_device().controller.info


def _wait_for_status(player, predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _device_status(player)
        if predicate(status):
            return status
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"timed out after {timeout:g}s waiting for {description}; "
        f"last device status {_device_status(player)!r}"
    )


def _playing(status):
    return status["player_state"] == "PLAYING"


def _our_playing(player, status):
    return (
        _playing(status)
        and player._current_session_id is not None
        and status["media_session_id"] == player._current_session_id
    )


def _stop_and_disconnect(player):
    try:
        player.stop()
    except Exception:
        logger.exception("Failed to stop playback during teardown")
    try:
        if player._device is not None:
            player._device._cast.disconnect()
    except Exception:
        logger.exception("Failed to disconnect from the speaker during teardown")


def test_search_and_stop_playback():
    """The quick smoke test: search, start playing, stop PLAY_SECONDS later."""
    player = castyt.Player()
    try:
        title, url = player.play_search(PLAY_QUERY)
        logger.info("Playing %r (%s)", title, url)
        _wait_for_status(
            player,
            lambda s: _our_playing(player, s),
            START_TIMEOUT_SECONDS,
            f"{title!r} to start playing",
        )

        time.sleep(PLAY_SECONDS)
        player.stop()

        _wait_for_status(
            player,
            lambda s: not _playing(s),
            STATUS_TIMEOUT_SECONDS,
            "playback to stop",
        )
        assert player.query is None
        assert player.queue == []
        assert player.index == -1
    finally:
        _stop_and_disconnect(player)


def test_play_announce_resume_then_advance_to_next_track():
    player = castyt.Player()
    try:
        announcement_url = announce.synthesize_and_serve(ANNOUNCEMENT_TEXT)
        assert tts.DEFAULT_PATH.stat().st_size > 0, "TTS produced an empty announcement file"

        title, url = player.play_search(QUERY)
        logger.info("Playing %r (%s)", title, url)
        started = _wait_for_status(
            player,
            lambda s: _our_playing(player, s),
            START_TIMEOUT_SECONDS,
            f"{title!r} to start playing",
        )
        track_title = started["media_metadata"].get("title")
        track_session_id = player._current_session_id

        time.sleep(INTERRUPT_AFTER_SECONDS)
        player.announce(announcement_url)
        interrupted_at = player._interrupted_at_seconds
        assert interrupted_at and interrupted_at > 0, "no interrupted position was recorded"

        _wait_for_status(
            player,
            lambda s: _playing(s) and s["content_id"] == announcement_url,
            STATUS_TIMEOUT_SECONDS,
            "the announcement to take over playback",
        )
        announcement_session_id = player._announcing_session_id
        assert announcement_session_id is not None, "announcement session id was never captured"
        assert announcement_session_id != track_session_id

        resumed = _wait_for_status(
            player,
            lambda s: not player._announcing and _our_playing(player, s),
            STATUS_TIMEOUT_SECONDS,
            "the interrupted track to resume after the announcement",
        )
        assert player.index == 0
        # Resuming re-resolves the stream, so the content id is a fresh signed
        # URL and only the queue entry and the device-reported title identify
        # the track.
        assert player.queue[0]["url"] == url, "resumed a different track"
        assert resumed["media_metadata"].get("title") == track_title, "resumed a different track"
        assert resumed["media_session_id"] != announcement_session_id
        position = resumed["current_time"]
        assert position > 0, "resumed from the start instead of the interrupted position"
        assert abs(position - interrupted_at) <= RESUME_DRIFT_ALLOWANCE_SECONDS, (
            f"resumed at {position:g}s but was interrupted at {interrupted_at:g}s"
        )

        next_status = _wait_for_status(
            player,
            lambda s: player.index == 1 and _our_playing(player, s),
            FINISH_TIMEOUT_SECONDS,
            "the first track to finish and the next one to auto-advance",
        )
        assert next_status["content_id"] == player._current_content_id
        assert player.queue[1]["url"] != url
        logger.info("Auto-advanced to %r", player.queue[player.index]["title"])

        player.stop()
        _wait_for_status(
            player,
            lambda s: not _playing(s),
            STATUS_TIMEOUT_SECONDS,
            "playback to stop",
        )
        assert player.query is None
        assert player.queue == []
        assert player.index == -1
    finally:
        _stop_and_disconnect(player)
