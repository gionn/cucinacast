"""Search YouTube and cast search results to a Chromecast device via catt, auto-advancing."""

import logging
import os
import random
import threading
import time

from catt.api import CattDevice
from catt.error import CastError
from catt.stream_info import StreamInfo
from yt_dlp import YoutubeDL

import storage

logger = logging.getLogger(__name__)

SEARCH_RESULT_COUNT = 5
SEARCH_POOL_SIZE = 20

DISCOVERY_RETRIES = 3
DISCOVERY_RETRY_DELAY_SECONDS = 3


def _nest_device_name():
    return os.environ.get("NEST_DEVICE_NAME")


def _get_device():
    last_error = None
    for attempt in range(DISCOVERY_RETRIES):
        try:
            name = _nest_device_name()
            return CattDevice(name) if name else CattDevice()
        except CastError as exc:
            last_error = exc
            if attempt < DISCOVERY_RETRIES - 1:
                time.sleep(DISCOVERY_RETRY_DELAY_SECONDS)
    raise last_error


def _fetch_live(query, fetch_count):
    opts = {"quiet": True, "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{fetch_count}:{query}", download=False)
        entries = info.get("entries") or []
        if not entries:
            raise ValueError(f"No YouTube results for {query!r}")
    return [
        {"id": e["id"], "title": e["title"], "view_count": e.get("view_count")}
        for e in entries
    ]


def search_youtube(query, count=SEARCH_RESULT_COUNT, exclude_ids=frozenset()):
    fetch_count = max(count, SEARCH_POOL_SIZE)
    entries = storage.cached_pool(query, fetch_count)
    if entries is None:
        entries = _fetch_live(query, fetch_count)
        storage.store_pool(query, entries)
    candidates = [e for e in entries if e["id"] not in exclude_ids]
    candidates.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
    top = candidates[:count]
    random.shuffle(top)
    return [
        {
            "id": e["id"],
            "title": e["title"],
            "url": f"https://www.youtube.com/watch?v={e['id']}",
        }
        for e in top
    ]


class Player:
    """Holds one persistent Chromecast connection and auto-advances through a queue."""

    def __init__(self):
        self._device = None
        self._lock = threading.Lock()
        self.query = None
        self.queue = []
        self.index = -1
        self._announcing = False
        self._track_started_at = 0
        self._interrupted_at_seconds = None

    def _ensure_device(self):
        if self._device is None:
            self._device = _get_device()
            self._device._cast.media_controller.register_status_listener(self)
        return self._device

    def _reset_cast_device_locked(self):
        """Drop the current connection so the next play attempt rediscovers
        the device from scratch. Needed because pychromecast's background
        reconnect thread can get stuck spinning on a Zeroconf instance that
        catt already stopped, silently keeping the connection dead forever."""
        if self._device is not None:
            try:
                self._device._cast.disconnect(blocking=False)
            except Exception:
                logger.exception("Error disconnecting stale Chromecast device")
            self._device = None

    def new_media_status(self, status):
        if status.player_state != "IDLE" or status.idle_reason != "FINISHED":
            return
        with self._lock:
            if self._announcing:
                self._announcing = False
                if 0 <= self.index < len(self.queue):
                    try:
                        self._play_current_locked(current_time=self._interrupted_at_seconds)
                        return
                    except CastError:
                        logger.exception(
                            "Failed to resume %r after announcement",
                            self.queue[self.index].get("title"),
                        )
                        self._reset_cast_device_locked()
                        self.index += 1
                    except Exception:
                        logger.exception(
                            "Failed to resume %r after announcement",
                            self.queue[self.index].get("title"),
                        )
                        self.index += 1
                    self._try_play_locked()
                return
            self._play_next_locked()

    def announce(self, url, content_type="audio/mpeg"):
        """Interrupt current playback (if any) to play a one-off announcement URL;
        the current queue entry resumes from roughly where it was interrupted
        once the announcement finishes (approximated via wall-clock elapsed
        time, not the device's actual playback position, so it can drift by a
        few seconds)."""
        with self._lock:
            self._announcing = True
            if 0 <= self.index < len(self.queue):
                self._interrupted_at_seconds = time.monotonic() - self._track_started_at
            else:
                self._interrupted_at_seconds = None
            device = self._ensure_device()
            try:
                device.controller.prep_app()
                device.controller.play_media_url(url, content_type=content_type)
            except Exception:
                logger.exception("Failed to play announcement")
                self._announcing = False

    def _play_current_locked(self, current_time=None):
        entry = self.queue[self.index]
        device = self._ensure_device()
        stream = StreamInfo(entry["url"], cast_info=device._cast.cast_info)
        device.controller.prep_app()
        device.controller.play_media_url(
            stream.video_url,
            title=stream.video_title,
            content_type=stream.guessed_content_type,
            thumb=stream.video_thumbnail,
            stream_type=stream.stream_type,
            current_time=current_time,
        )
        self._track_started_at = time.monotonic() - (current_time or 0)

    def _play_next_locked(self):
        self.index += 1
        self._try_play_locked()

    def _try_play_locked(self, max_attempts=20):
        """Try to play the entry at self.index, skipping unplayable ones and
        growing the queue as needed. Returns the entry that started playing,
        or None if nothing playable was found."""
        for _ in range(max_attempts):
            if self.index >= len(self.queue):
                self._grow_queue_locked()
                if self.index >= len(self.queue):
                    return None
            entry = self.queue[self.index]
            try:
                self._play_current_locked()
                storage.record_play(self.query, entry["id"])
                return entry
            except CastError:
                logger.exception("Skipping unplayable track %r", entry.get("title"))
                self._reset_cast_device_locked()
                self.index += 1
            except Exception:
                logger.exception("Skipping unplayable track %r", entry.get("title"))
                self.index += 1
        return None

    def _grow_queue_locked(self):
        seen_ids = {e["id"] for e in self.queue}
        exclude_ids = seen_ids | storage.recent_ids(self.query)
        try:
            entries = search_youtube(
                self.query, count=len(self.queue) + SEARCH_RESULT_COUNT, exclude_ids=exclude_ids
            )
        except Exception:
            logger.exception("Failed to fetch more search results for %r", self.query)
            return
        self.queue.extend(entries)

    def play_search(self, query):
        entries = search_youtube(
            query, count=SEARCH_POOL_SIZE, exclude_ids=storage.recent_ids(query)
        )
        with self._lock:
            self.query = query
            self.queue = entries
            self.index = 0
            self._announcing = False  # a fresh /play supersedes any in-flight announcement
            played = self._try_play_locked()
        if not played:
            raise RuntimeError(f"No playable YouTube results for {query!r}")
        return played["title"], played["url"]

    def stop(self):
        with self._lock:
            self.query = None
            self.queue = []
            self.index = -1
            self._announcing = False
            self._ensure_device().stop()


player = Player()
