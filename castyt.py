"""Search YouTube and cast search results to a Chromecast device via catt, auto-advancing."""
import logging
import os
import threading
import time

from catt.api import CattDevice
from catt.error import CastError
from catt.stream_info import StreamInfo
from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)

NEST_DEVICE_NAME = os.environ.get("NEST_DEVICE_NAME")
SEARCH_RESULT_COUNT = 5

DISCOVERY_RETRIES = 3
DISCOVERY_RETRY_DELAY_SECONDS = 3


def _get_device():
    last_error = None
    for attempt in range(DISCOVERY_RETRIES):
        try:
            return CattDevice(NEST_DEVICE_NAME) if NEST_DEVICE_NAME else CattDevice()
        except CastError as exc:
            last_error = exc
            if attempt < DISCOVERY_RETRIES - 1:
                time.sleep(DISCOVERY_RETRY_DELAY_SECONDS)
    raise last_error


def search_youtube(query, count=SEARCH_RESULT_COUNT):
    opts = {"quiet": True, "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
        entries = info.get("entries") or []
        if not entries:
            raise ValueError(f"No YouTube results for {query!r}")
    return [
        {
            "id": e["id"],
            "title": e["title"],
            "url": f"https://www.youtube.com/watch?v={e['id']}",
        }
        for e in entries
    ]


class Player:
    """Holds one persistent Chromecast connection and auto-advances through a queue."""

    def __init__(self):
        self._device = None
        self._lock = threading.Lock()
        self.query = None
        self.queue = []
        self.index = -1

    def _ensure_device(self):
        if self._device is None:
            self._device = _get_device()
            self._device._cast.media_controller.register_status_listener(self)
        return self._device

    def new_media_status(self, status):
        if status.player_state == "IDLE" and status.idle_reason == "FINISHED":
            with self._lock:
                self._play_next_locked()

    def _play_current_locked(self):
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
        )

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
                return entry
            except Exception:
                logger.exception("Skipping unplayable track %r", entry.get("title"))
                self.index += 1
        return None

    def _grow_queue_locked(self):
        seen_ids = {e["id"] for e in self.queue}
        try:
            entries = search_youtube(self.query, count=len(self.queue) + SEARCH_RESULT_COUNT)
        except Exception:
            logger.exception("Failed to fetch more search results for %r", self.query)
            return
        new_entries = [e for e in entries if e["id"] not in seen_ids]
        self.queue.extend(new_entries)

    def play_search(self, query):
        entries = search_youtube(query)
        with self._lock:
            self.query = query
            self.queue = entries
            self.index = 0
            played = self._try_play_locked()
        if not played:
            raise RuntimeError(f"No playable YouTube results for {query!r}")
        return played["title"], played["url"]

    def stop(self):
        with self._lock:
            self.query = None
            self.queue = []
            self.index = -1
            self._ensure_device().stop()


player = Player()
