"""Search YouTube and cast search results to a Chromecast device via catt, auto-advancing."""

import logging
import os
import random
import threading
import time

import zeroconf
from catt.api import CattDevice
from catt.discovery import get_cast_infos
from catt.error import CastError
from catt.stream_info import StreamInfo
from yt_dlp import YoutubeDL

import storage

logger = logging.getLogger(__name__)

SEARCH_RESULT_COUNT = 5
SEARCH_POOL_SIZE = 20

DISCOVERY_RETRIES = 3
DISCOVERY_RETRY_DELAY_SECONDS = 3


class DeviceUnavailable(Exception):
    """The speaker could not be reached, as opposed to a track that failed."""


def _nest_device_name():
    return os.environ.get("NEST_DEVICE_NAME")


def _discover_cast():
    """Return the host of the first Chromecast found on the LAN, for installs that
    don't pin NEST_DEVICE_NAME. Sorted so several speakers always resolve to the
    same one."""
    infos = sorted(get_cast_infos(), key=lambda info: info.friendly_name)
    if not infos:
        raise CastError("No Chromecast found on the network")
    return infos[0].host


def _get_device():
    last_error = None
    for attempt in range(DISCOVERY_RETRIES):
        try:
            name = _nest_device_name()
            return CattDevice(name) if name else CattDevice(ip_addr=_discover_cast())
        except CastError as exc:
            last_error = exc
            if attempt < DISCOVERY_RETRIES - 1:
                time.sleep(DISCOVERY_RETRY_DELAY_SECONDS)
    name = _nest_device_name()
    target = f"Chromecast {name!r}" if name else "Chromecast"
    raise DeviceUnavailable(f"{target} could not be found on the network") from last_error


def _is_our_session(finished_content_id, finished_session_id, content_id, session_id):
    """Whether a FINISHED status can be proven to belong to a session we started.
    A reported session id is authoritative; the content id is only a fallback when
    one side has no session id. Announcements reuse one URL, so a conflicting
    session id must not be overridden by a matching content id."""
    if session_id is not None and finished_session_id is not None:
        return finished_session_id == session_id
    return content_id is not None and finished_content_id == content_id


def _fetch_live(query, fetch_count):
    opts = {"quiet": True, "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{fetch_count}:{query}", download=False)
        entries = info.get("entries") or []
        if not entries:
            raise ValueError(f"No YouTube results for {query!r}")
    return [
        {"id": e["id"], "title": e["title"], "view_count": e.get("view_count")} for e in entries
    ]


def search_youtube(query, count=SEARCH_RESULT_COUNT, exclude_ids=frozenset()):
    fetch_count = max(count, SEARCH_POOL_SIZE) + len(exclude_ids)
    entries = storage.cached_pool(query, count)
    candidates = [e for e in (entries or []) if e["id"] not in exclude_ids]
    if entries is None or len(candidates) < count:
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
        self._zconf = None
        self._lock = threading.Lock()
        self.query = None
        self.queue = []
        self.index = -1
        self._announcing = False
        self._announcing_content_id = None
        self._announcing_session_id = None
        self._track_started_at = 0
        self._interrupted_at_seconds = None
        self._current_content_id = None
        self._current_session_id = None

    def _clear_announcing(self):
        self._announcing = False
        self._announcing_content_id = None
        self._announcing_session_id = None

    def _live_zconf(self):
        """Return a persistent zeroconf instance, created once and never
        closed. catt hands each Chromecast the browser's shared zeroconf and
        then stops it, so pychromecast's reconnect thread would resolve
        services against a dead loop and spin forever."""
        if self._zconf is None:
            self._zconf = zeroconf.Zeroconf()
        return self._zconf

    def _ensure_device(self):
        if self._device is None:
            self._device = _get_device()
            self._device._cast.socket_client.zconf = self._live_zconf()
            self._device._cast.media_controller.register_status_listener(self)
        return self._device

    def _reset_cast_device_locked(self):
        """Drop the current connection so the next play attempt rediscovers
        the device from scratch."""
        if self._device is not None:
            try:
                self._device._cast.disconnect(timeout=0)
            except Exception:
                logger.exception("Error disconnecting stale Chromecast device")
            self._device = None

    def _capture_session_id(self, status):
        """Record the session id of the media we just loaded, once the device
        reports it as current. catt's `block_until_active` only waits for *any*
        active session, which can still be the previous one, so reading the id
        straight after `play_media_url` can pick up a stale value and make our
        own media's FINISHED look like someone else's."""
        content_id = getattr(status, "content_id", None)
        session_id = getattr(status, "media_session_id", None)
        if content_id is None or not isinstance(session_id, int):
            return
        if getattr(status, "player_state", None) == "IDLE":
            # A terminal status belongs to the session that just ended, which may
            # be an announcement this one replaced; claiming its id would make our
            # own FINISHED fail the match.
            return
        with self._lock:
            if (
                self._announcing
                and self._announcing_session_id is None
                and content_id == self._announcing_content_id
            ):
                self._announcing_session_id = session_id
                logger.info("Announcement running as session %s", session_id)
            elif self._current_session_id is None and content_id == self._current_content_id:
                self._current_session_id = session_id
                logger.info("Track running as session %s", session_id)

    def new_media_status(self, status):
        if status.player_state != "IDLE" or status.idle_reason != "FINISHED":
            self._capture_session_id(status)
            return
        with self._lock:
            finished_content_id = getattr(status, "content_id", None)
            finished_session_id = getattr(status, "media_session_id", None)
            if self._announcing:
                if not _is_our_session(
                    finished_content_id,
                    finished_session_id,
                    self._announcing_content_id,
                    self._announcing_session_id,
                ):
                    # Can't prove this finish is our announcement, so the user replaced
                    # it; drop the resume rather than firing it over their media.
                    logger.info(
                        "Finished session %s (content %r) is not our announcement "
                        "session %s (content %r); dropping resume",
                        finished_session_id,
                        finished_content_id,
                        self._announcing_session_id,
                        self._announcing_content_id,
                    )
                    self._clear_announcing()
                    return
                self._clear_announcing()
                if 0 <= self.index < len(self.queue):
                    try:
                        self._play_current_locked(current_time=self._interrupted_at_seconds)
                        return
                    except DeviceUnavailable:
                        logger.warning(
                            "Speaker unavailable; not resuming %r after announcement",
                            self.queue[self.index].get("title"),
                        )
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
                    try:
                        self._try_play_locked()
                    except DeviceUnavailable:
                        logger.warning("Speaker unavailable; not resuming queue")
                return
            if self.query is None or not self.queue:
                # new_media_status sees every session on the device, not just
                # ours, so a FINISHED can belong to media the user started.
                logger.info("No active playback queue; ignoring finished media")
                return
            if not _is_our_session(
                finished_content_id,
                finished_session_id,
                self._current_content_id,
                self._current_session_id,
            ):
                # Can't prove the finished session is the one we started, so it isn't
                # ours to advance.
                logger.info("Finished session is not the track we are playing; ignoring")
                return
            try:
                self._play_next_locked()
            except DeviceUnavailable:
                logger.warning("Speaker unavailable; not advancing queue")

    def announce(self, url, content_type="audio/mpeg"):
        """Interrupt current playback (if any) to play a one-off announcement URL;
        the current queue entry resumes from roughly where it was interrupted
        once the announcement finishes (approximated via wall-clock elapsed
        time, not the device's actual playback position, so it can drift by a
        few seconds)."""
        with self._lock:
            self._announcing = True
            self._announcing_content_id = url
            self._announcing_session_id = None  # filled in by _capture_session_id
            if 0 <= self.index < len(self.queue):
                self._interrupted_at_seconds = time.monotonic() - self._track_started_at
            else:
                self._interrupted_at_seconds = None
            try:
                device = self._ensure_device()
                device.controller.prep_app()
                device.controller.play_media_url(
                    url, content_type=content_type, stream_type="BUFFERED"
                )
            except DeviceUnavailable:
                self._clear_announcing()
                raise
            except Exception:
                logger.exception("Failed to play announcement")
                self._clear_announcing()

    def _play_current_locked(self, current_time=None):
        entry = self.queue[self.index]
        # A failed attempt must not leave the previous track's ids looking like ours.
        self._current_content_id = None
        self._current_session_id = None
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
        self._current_content_id = stream.video_url  # session id captured by _capture_session_id

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
            except DeviceUnavailable:
                # An unreachable speaker isn't a bad track; don't skip through
                # the queue rediscovering for every entry.
                logger.warning("Speaker unavailable, aborting playback attempt")
                raise
            except CastError:
                logger.exception("Skipping unplayable track %r", entry.get("title"))
                self._reset_cast_device_locked()
                self.index += 1
            except Exception:
                logger.exception("Skipping unplayable track %r", entry.get("title"))
                self.index += 1
        return None

    def _grow_queue_locked(self):
        if not self.query:
            # Without a query this would become a literal "ytsearchN:None" search.
            logger.warning("Not growing queue: no active query")
            return
        seen_ids = {e["id"] for e in self.queue}
        exclude_ids = seen_ids | storage.recent_ids(self.query)
        try:
            entries = search_youtube(self.query, count=SEARCH_RESULT_COUNT, exclude_ids=exclude_ids)
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
            self._clear_announcing()  # a fresh /play supersedes any in-flight announcement
            played = self._try_play_locked()
        if not played:
            raise RuntimeError(f"No playable YouTube results for {query!r}")
        return played["title"], played["url"]

    def skip(self):
        with self._lock:
            if not (0 <= self.index < len(self.queue)):
                raise RuntimeError("Nothing is playing.")
            self._clear_announcing()
            self.index += 1
            played = self._try_play_locked()
        if not played:
            raise RuntimeError("No more tracks to play.")
        return played["title"], played["url"]

    def now_playing(self):
        with self._lock:
            if not (0 <= self.index < len(self.queue)):
                return None
            entry = self.queue[self.index]
            device = self._device
        state = None
        if device is not None:
            try:
                state = device.controller.info.get("player_state")
            except Exception:
                logger.exception("Failed to read player state")
        return entry["title"], entry["url"], state

    def stop(self):
        with self._lock:
            self.query = None
            self.queue = []
            self.index = -1
            self._clear_announcing()
            try:
                self._ensure_device().stop()
            except DeviceUnavailable:
                # Always clear our own state, even if the speaker can't be reached.
                logger.warning("Speaker unavailable while stopping; cleared queue state")


player = Player()
