import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from catt.error import CastError

import castyt
import storage


def _fake_entries():
    return [
        {"id": "low", "title": "Low views", "view_count": 10},
        {"id": "high", "title": "High views", "view_count": 1000},
        {"id": "mid", "title": "Mid views", "view_count": 500},
    ]


def test_search_youtube_picks_top_count_by_view_count(monkeypatch):
    monkeypatch.setattr(castyt, "_fetch_live", lambda query, fetch_count: _fake_entries())
    monkeypatch.setattr(castyt.random, "shuffle", lambda seq: None)

    results = castyt.search_youtube("query", count=2)

    assert {e["id"] for e in results} == {"high", "mid"}


def test_search_youtube_excludes_ids(monkeypatch):
    monkeypatch.setattr(castyt, "_fetch_live", lambda query, fetch_count: _fake_entries())

    results = castyt.search_youtube("query", count=3, exclude_ids={"high"})

    assert "high" not in {e["id"] for e in results}


def test_search_youtube_uses_cache_on_second_call(monkeypatch):
    pool_size = len(_fake_entries())
    monkeypatch.setattr(castyt, "SEARCH_POOL_SIZE", pool_size)
    calls = []

    def fake_fetch(query, fetch_count):
        calls.append(query)
        return _fake_entries()

    monkeypatch.setattr(castyt, "_fetch_live", fake_fetch)

    castyt.search_youtube("query", count=pool_size)
    castyt.search_youtube("query", count=pool_size)

    assert calls == ["query"]


def test_search_youtube_uses_cache_for_sparse_query(monkeypatch):
    """A query with fewer results than SEARCH_POOL_SIZE should still hit cache
    on repeat calls, as long as it has enough rows for the requested count."""
    calls = []

    def fake_fetch(query, fetch_count):
        calls.append(query)
        return _fake_entries()  # fewer than SEARCH_POOL_SIZE

    monkeypatch.setattr(castyt, "_fetch_live", fake_fetch)

    castyt.search_youtube("query", count=2)
    castyt.search_youtube("query", count=2)

    assert calls == ["query"]


def test_search_youtube_refetches_when_exclusions_exhaust_cache(monkeypatch):
    """If exclude_ids filters the cached pool down below count, search_youtube
    must fall back to a live fetch instead of returning too few results."""
    calls = []

    def fake_fetch(query, fetch_count):
        calls.append(fetch_count)
        return _fake_entries()

    monkeypatch.setattr(castyt, "_fetch_live", fake_fetch)

    castyt.search_youtube("query", count=2)
    results = castyt.search_youtube("query", count=2, exclude_ids={"high", "mid"})

    assert len(calls) == 2
    assert {e["id"] for e in results} == {"low"}


def test_search_youtube_returns_urls(monkeypatch):
    monkeypatch.setattr(castyt, "_fetch_live", lambda query, fetch_count: _fake_entries())

    results = castyt.search_youtube("query", count=1)

    assert results[0]["url"] == f"https://www.youtube.com/watch?v={results[0]['id']}"


def test_search_youtube_raises_when_no_results(monkeypatch):
    def raise_no_results(query, fetch_count):
        raise ValueError(f"No YouTube results for {query!r}")

    monkeypatch.setattr(castyt, "_fetch_live", raise_no_results)

    with pytest.raises(ValueError):
        castyt.search_youtube("nonexistent query")


class _FakeDevice:
    def __init__(self):
        self.controller = Mock()
        self._cast = Mock()
        self._cast.cast_info = "fake-cast-info"
        self.stop = Mock()


class _FakeStreamInfo:
    def __init__(self, url, cast_info=None):
        self.url = url
        self.video_url = url
        self.video_title = "Title"
        self.guessed_content_type = "audio/mpeg"
        self.video_thumbnail = None
        self.stream_type = "BUFFERED"


def _media_status(content_id=None, state="PLAYING", idle_reason=None):
    return SimpleNamespace(
        player_state=state,
        idle_reason=idle_reason,
        content_id=content_id,
    )


_FINISHED_STATUS = _media_status(state="IDLE", idle_reason="FINISHED")


def test_new_media_status_ignores_non_finished_status():
    player = castyt.Player()
    player._play_next_locked = Mock()

    player.new_media_status(SimpleNamespace(player_state="BUFFERING", idle_reason=None))

    player._play_next_locked.assert_not_called()


def test_new_media_status_ignores_idle_with_non_finished_reason():
    # Stopping media by hand reports CANCELLED/INTERRUPTED, not FINISHED; it must
    # not advance the queue (even with an active one).
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._play_next_locked = Mock()

    player.new_media_status(
        SimpleNamespace(player_state="IDLE", idle_reason="CANCELLED", content_id="x")
    )

    player._play_next_locked.assert_not_called()


def test_new_media_status_ignores_finished_without_active_queue():
    # A FINISHED status from media the user started directly on the device (or a
    # stale session) must not make the bot start casting on its own.
    player = castyt.Player()
    player._play_next_locked = Mock()

    player.new_media_status(_FINISHED_STATUS)

    player._play_next_locked.assert_not_called()


def test_new_media_status_advances_queue_when_not_announcing():
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "u"
    player._play_next_locked = Mock()

    player.new_media_status(
        SimpleNamespace(player_state="IDLE", idle_reason="FINISHED", content_id="u")
    )

    player._play_next_locked.assert_called_once()


def test_new_media_status_ignores_finished_without_content_id():
    # Without a content id there is no proof the finished media is ours.
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "u"
    player._play_next_locked = Mock()

    player.new_media_status(_FINISHED_STATUS)  # content_id is None

    player._play_next_locked.assert_not_called()


def test_new_media_status_ignores_foreign_media_when_queue_active():
    # Even with an active queue, a finished session that isn't the track we
    # loaded (e.g. the user's news) must not be treated as ours to advance.
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "https://www.youtube.com/watch?v=ours"
    player._play_next_locked = Mock()

    player.new_media_status(
        SimpleNamespace(
            player_state="IDLE", idle_reason="FINISHED", content_id="https://news.example/other"
        )
    )

    player._play_next_locked.assert_not_called()


def test_new_media_status_advances_when_content_id_matches():
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "https://www.youtube.com/watch?v=ours"
    player._play_next_locked = Mock()

    player.new_media_status(
        SimpleNamespace(
            player_state="IDLE",
            idle_reason="FINISHED",
            content_id="https://www.youtube.com/watch?v=ours",
        )
    )

    player._play_next_locked.assert_called_once()


def _player_with_device(monkeypatch):
    fake_device = _FakeDevice()
    monkeypatch.setattr(castyt, "_get_device", lambda: fake_device)
    player = castyt.Player()
    player._ensure_device()
    listener = fake_device._cast.media_controller.register_status_listener.call_args.args[0]
    return player, listener


def test_media_status_listener_does_not_block_on_playback_lock(monkeypatch):
    # play_search/announce hold _lock while blocked on socket responses, and the
    # listener runs on that same socket thread, so handling a status inline would
    # stall the load it is waiting for.
    player, listener = _player_with_device(monkeypatch)
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "u"
    player._play_next_locked = Mock()

    player._lock.acquire()
    try:
        caller = threading.Thread(
            target=listener.new_media_status,
            args=(_media_status(content_id="u", state="IDLE", idle_reason="FINISHED"),),
            daemon=True,
        )
        caller.start()
        caller.join(1.0)
        assert not caller.is_alive(), "status handling blocked on the playback lock"
        player._play_next_locked.assert_not_called()
    finally:
        player._lock.release()

    player._status_dispatcher.drain()
    player._play_next_locked.assert_called_once()


def test_status_dispatcher_snapshots_status_before_handling(monkeypatch):
    # pychromecast rewrites one MediaStatus in place and fires listeners with it,
    # so a queued reference would change under the worker while it waits for the
    # lock, losing the status it was queued for.
    player, listener = _player_with_device(monkeypatch)
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "u"
    player._play_next_locked = Mock()

    status = _media_status(content_id="u", state="IDLE", idle_reason="FINISHED")
    player._lock.acquire()
    try:
        listener.new_media_status(status)
        status.content_id = "other"  # the device's next update, same object
    finally:
        player._lock.release()

    player._status_dispatcher.drain()
    player._play_next_locked.assert_called_once()


def test_new_media_status_resumes_after_announcement():
    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3?t=first"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._interrupted_at_seconds = 12.3
    player._play_current_locked = Mock()
    player._play_next_locked = Mock()

    player.new_media_status(
        _media_status(
            content_id="http://host/announce.mp3?t=first", state="IDLE", idle_reason="FINISHED"
        )
    )

    player._play_current_locked.assert_called_once_with(current_time=12.3)
    assert player._announcing_content_id is None
    player._play_next_locked.assert_not_called()


def test_new_media_status_ignores_finish_of_replaced_announcement(monkeypatch):
    # A replaced announcement's FINISHED can arrive after the replacement loaded.
    # Its URL differs, so it must neither resume nor clear the new announcement.
    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3?t=second"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._interrupted_at_seconds = 12.3
    player._play_current_locked = Mock()
    player._try_play_locked = Mock()

    player.new_media_status(
        _media_status(
            content_id="http://host/announce.mp3?t=first", state="IDLE", idle_reason="FINISHED"
        )
    )

    player._play_current_locked.assert_not_called()
    player._try_play_locked.assert_not_called()
    assert player._announcing_content_id == "http://host/announce.mp3?t=second"


def test_new_media_status_ignores_finish_of_media_while_announcing():
    # The user replaced the announcement with their own media, so the interrupted
    # track must not resume over it.
    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3?t=first"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._interrupted_at_seconds = 12.3
    player._play_current_locked = Mock()
    player._try_play_locked = Mock()

    player.new_media_status(
        SimpleNamespace(
            player_state="IDLE",
            idle_reason="FINISHED",
            content_id="http://news.example/other",
        )
    )

    player._play_current_locked.assert_not_called()
    player._try_play_locked.assert_not_called()


def test_new_media_status_resets_device_on_cast_error_resuming():
    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._play_current_locked = Mock(side_effect=CastError("fail"))
    player._reset_cast_device_locked = Mock()
    player._try_play_locked = Mock()

    player.new_media_status(
        SimpleNamespace(
            player_state="IDLE", idle_reason="FINISHED", content_id="http://host/announce.mp3"
        )
    )

    player._reset_cast_device_locked.assert_called_once()
    assert player.index == 1
    player._try_play_locked.assert_called_once()


def test_new_media_status_falls_back_without_reset_on_generic_error_resuming():
    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._play_current_locked = Mock(side_effect=RuntimeError("boom"))
    player._reset_cast_device_locked = Mock()
    player._try_play_locked = Mock()

    player.new_media_status(
        SimpleNamespace(
            player_state="IDLE", idle_reason="FINISHED", content_id="http://host/announce.mp3"
        )
    )

    player._reset_cast_device_locked.assert_not_called()
    assert player.index == 1
    player._try_play_locked.assert_called_once()


def test_playing_a_track_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(castyt, "_get_device", lambda: _FakeDevice())
    monkeypatch.setattr(castyt, "StreamInfo", _FakeStreamInfo)
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "Da Funk", "url": "u"}]
    player.index = 0

    with caplog.at_level(logging.INFO, logger="castyt"):
        player._try_play_locked()

    assert "Playing 'Da Funk'" in caplog.text


def test_resume_after_announcement_is_logged(caplog):
    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3?t=first"
    player.queue = [{"id": "a", "title": "Da Funk", "url": "u"}]
    player.index = 0
    player._interrupted_at_seconds = 12.3
    player._play_current_locked = Mock()

    with caplog.at_level(logging.INFO, logger="castyt"):
        player.new_media_status(
            _media_status(
                content_id="http://host/announce.mp3?t=first", state="IDLE", idle_reason="FINISHED"
            )
        )

    assert "Resumed 'Da Funk' at 12.3s" in caplog.text


def test_idle_status_while_announcing_is_logged(caplog):
    # An idle status with no terminal meaning is otherwise invisible, and it is the
    # only clue when an announcement ends early.
    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3?t=first"

    with caplog.at_level(logging.INFO, logger="castyt"):
        player.new_media_status(
            _media_status(content_id="http://host/x.mp3", state="IDLE", idle_reason="INTERRUPTED")
        )

    assert "Idle (INTERRUPTED) while announcing" in caplog.text


def test_announce_logs_the_track_it_interrupts(monkeypatch, caplog):
    monkeypatch.setattr(castyt, "_get_device", lambda: _FakeDevice())
    player = castyt.Player()
    player.queue = [{"id": "a", "title": "Da Funk", "url": "u"}]
    player.index = 0

    with caplog.at_level(logging.INFO, logger="castyt"):
        player.announce("http://host/announce.mp3?t=first")

    assert "Announcing over 'Da Funk'" in caplog.text


def test_failed_announcement_resumes_the_track():
    # A media the device gives up on never sends FINISHED, so the queue would stay
    # interrupted forever.
    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3?t=first"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._interrupted_at_seconds = 12.3
    player._play_current_locked = Mock()

    player.new_media_status(
        _media_status(
            content_id="http://host/announce.mp3?t=first", state="IDLE", idle_reason="ERROR"
        )
    )

    player._play_current_locked.assert_called_once_with(current_time=12.3)
    assert player._announcing_content_id is None


def test_failed_media_of_other_content_does_not_resume():
    # The ERROR may belong to media we never loaded, so it is not ours to end.
    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3?t=first"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._play_current_locked = Mock()

    player.new_media_status(
        _media_status(content_id="http://news.example/other", state="IDLE", idle_reason="ERROR")
    )

    player._play_current_locked.assert_not_called()
    assert player._announcing_content_id == "http://host/announce.mp3?t=first"


def test_ensure_device_injects_live_zeroconf(monkeypatch):
    fake_device = _FakeDevice()
    sentinel_zconf = object()
    monkeypatch.setattr(castyt, "_get_device", lambda: fake_device)
    monkeypatch.setattr(castyt.zeroconf, "Zeroconf", lambda: sentinel_zconf)
    player = castyt.Player()

    player._ensure_device()

    assert player._device is fake_device
    assert fake_device._cast.socket_client.zconf is sentinel_zconf
    fake_device._cast.media_controller.register_status_listener.assert_called_once_with(
        player._status_dispatcher
    )


def test_ensure_device_reuses_zeroconf_after_reset(monkeypatch):
    devices = [_FakeDevice(), _FakeDevice()]
    first_zconf = object()
    second_zconf = object()
    call_count = 0

    def fake_zeroconf():
        nonlocal call_count
        call_count += 1
        return first_zconf if call_count == 1 else second_zconf

    monkeypatch.setattr(castyt, "_get_device", Mock(side_effect=devices))
    monkeypatch.setattr(castyt.zeroconf, "Zeroconf", fake_zeroconf)
    player = castyt.Player()

    player._ensure_device()
    player._reset_cast_device_locked()
    player._ensure_device()

    assert devices[1]._cast.socket_client.zconf is first_zconf
    assert call_count == 1


def test_announce_captures_interrupted_position_and_plays_url(monkeypatch):
    player = castyt.Player()
    fake_device = _FakeDevice()
    monkeypatch.setattr(castyt, "_get_device", lambda: fake_device)
    monkeypatch.setattr(castyt.time, "monotonic", lambda: 105.0)
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._track_started_at = 100.0

    player.announce("http://host/audio.mp3")

    assert player._interrupted_at_seconds == 5.0
    assert player._announcing_content_id == "http://host/audio.mp3"
    fake_device.controller.play_media_url.assert_called_once_with(
        "http://host/audio.mp3", content_type="audio/mpeg", stream_type="BUFFERED"
    )


def test_announce_clears_announcing_flag_on_failure(monkeypatch):
    player = castyt.Player()
    fake_device = _FakeDevice()
    fake_device.controller.play_media_url.side_effect = RuntimeError("boom")
    monkeypatch.setattr(castyt, "_get_device", lambda: fake_device)

    player.announce("http://host/audio.mp3")

    assert player._announcing_content_id is None


def test_announce_clears_state_and_reraises_on_device_unavailable(monkeypatch):
    monkeypatch.setattr(castyt, "_get_device", Mock(side_effect=castyt.DeviceUnavailable("nope")))
    player = castyt.Player()
    player._announcing_content_id = "old"

    with pytest.raises(castyt.DeviceUnavailable):
        player.announce("http://host/audio.mp3")

    assert player._announcing_content_id is None


def test_try_play_locked_records_play_on_success(monkeypatch):
    monkeypatch.setattr(castyt, "_get_device", lambda: _FakeDevice())
    monkeypatch.setattr(castyt, "StreamInfo", _FakeStreamInfo)
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0

    entry = player._try_play_locked()

    assert entry["id"] == "a"
    assert storage.recent_ids("daft punk") == {"a"}


def test_try_play_locked_skips_entry_on_cast_error(monkeypatch):
    devices = [_FakeDevice(), _FakeDevice()]
    devices[0].controller.play_media_url.side_effect = CastError("fail")
    monkeypatch.setattr(castyt, "_get_device", Mock(side_effect=devices))
    monkeypatch.setattr(castyt, "StreamInfo", _FakeStreamInfo)

    player = castyt.Player()
    player.query = "q"
    player.queue = [
        {"id": "a", "title": "A", "url": "u1"},
        {"id": "b", "title": "B", "url": "u2"},
    ]
    player.index = 0

    entry = player._try_play_locked()

    assert entry["id"] == "b"
    assert player.index == 1
    devices[0]._cast.disconnect.assert_called_once_with(timeout=0)


def test_try_play_locked_skips_entry_on_generic_error_without_reset(monkeypatch):
    # A generic exception (unlike CastError) doesn't reset the device, so the
    # same device instance is reused for the next attempt.
    device = _FakeDevice()
    device.controller.play_media_url.side_effect = [RuntimeError("boom"), None]
    monkeypatch.setattr(castyt, "_get_device", lambda: device)
    monkeypatch.setattr(castyt, "StreamInfo", _FakeStreamInfo)

    player = castyt.Player()
    player.query = "q"
    player.queue = [
        {"id": "a", "title": "A", "url": "u1"},
        {"id": "b", "title": "B", "url": "u2"},
    ]
    player.index = 0

    entry = player._try_play_locked()

    assert entry["id"] == "b"
    assert player.index == 1
    device._cast.disconnect.assert_not_called()


def test_try_play_locked_returns_none_when_exhausted(monkeypatch):
    fake_device = _FakeDevice()
    fake_device.controller.play_media_url.side_effect = RuntimeError("boom")
    monkeypatch.setattr(castyt, "_get_device", lambda: fake_device)
    monkeypatch.setattr(castyt, "StreamInfo", _FakeStreamInfo)

    player = castyt.Player()
    player.query = "q"
    player.queue = [
        {"id": "a", "title": "A", "url": "u1"},
        {"id": "b", "title": "B", "url": "u2"},
    ]
    player.index = 0
    player._grow_queue_locked = Mock()

    result = player._try_play_locked(max_attempts=5)

    assert result is None
    player._grow_queue_locked.assert_called_once()


def test_grow_queue_locked_excludes_queued_and_recent_ids(monkeypatch):
    storage.record_play("q", "recent-1")
    calls = []

    def fake_search_youtube(query, count, exclude_ids=frozenset()):
        calls.append((query, count, exclude_ids))
        return [{"id": "new", "title": "New", "url": "u"}]

    monkeypatch.setattr(castyt, "search_youtube", fake_search_youtube)

    player = castyt.Player()
    player.query = "q"
    player.queue = [{"id": "already-queued", "title": "X", "url": "u"}]

    player._grow_queue_locked()

    assert len(calls) == 1
    _, count, exclude_ids = calls[0]
    assert count == castyt.SEARCH_RESULT_COUNT
    assert exclude_ids == {"already-queued", "recent-1"}
    assert player.queue[-1]["id"] == "new"


def test_grow_queue_locked_swallows_search_errors(monkeypatch):
    def raise_error(query, count, exclude_ids=frozenset()):
        raise RuntimeError("network down")

    monkeypatch.setattr(castyt, "search_youtube", raise_error)

    player = castyt.Player()
    player.query = "q"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]

    player._grow_queue_locked()  # must not raise

    assert len(player.queue) == 1


def test_grow_queue_locked_noop_without_query(monkeypatch):
    # A None/empty query must never become a live "ytsearchN:None" search.
    calls = []

    def fake_search_youtube(query, count, exclude_ids=frozenset()):
        calls.append(query)
        return [{"id": "new", "title": "New", "url": "u"}]

    monkeypatch.setattr(castyt, "search_youtube", fake_search_youtube)

    player = castyt.Player()
    player.query = None
    player.queue = []

    player._grow_queue_locked()

    assert calls == []
    assert player.queue == []


def test_play_current_locked_records_content_id(monkeypatch):
    monkeypatch.setattr(castyt, "_get_device", lambda: _FakeDevice())
    monkeypatch.setattr(castyt, "StreamInfo", _FakeStreamInfo)
    player = castyt.Player()
    player.query = "q"
    player.queue = [{"id": "a", "title": "A", "url": "u1"}]
    player.index = 0

    player._play_current_locked()

    assert player._current_content_id == "u1"


def test_play_current_locked_clears_ownership_id_on_failure(monkeypatch):
    monkeypatch.setattr(castyt, "_get_device", Mock(side_effect=castyt.DeviceUnavailable("nope")))
    player = castyt.Player()
    player.query = "q"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "stale"

    with pytest.raises(castyt.DeviceUnavailable):
        player._play_current_locked()

    assert player._current_content_id is None


def test_play_search_raises_when_nothing_playable(monkeypatch):
    monkeypatch.setattr(
        castyt, "search_youtube", lambda query, count=0, exclude_ids=frozenset(): _fake_entries()
    )
    player = castyt.Player()
    player._try_play_locked = Mock(return_value=None)

    with pytest.raises(RuntimeError):
        player.play_search("some query")


def test_stop_resets_player_state(monkeypatch):
    fake_device = _FakeDevice()
    monkeypatch.setattr(castyt, "_get_device", lambda: fake_device)

    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3?t=abc"
    player.query = "q"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0

    player.stop()

    assert player.query is None
    assert player.queue == []
    assert player.index == -1
    assert player._announcing_content_id is None
    fake_device.stop.assert_called_once()


def test_get_device_uses_the_configured_name(monkeypatch):
    monkeypatch.setenv("NEST_DEVICE_NAME", "Cucinino")
    monkeypatch.setattr(castyt, "CattDevice", Mock(return_value=Mock()))

    castyt._get_device()

    castyt.CattDevice.assert_called_once_with("Cucinino")


def test_get_device_discovers_a_cast_when_no_name_is_set(monkeypatch):
    monkeypatch.delenv("NEST_DEVICE_NAME", raising=False)
    monkeypatch.setattr(
        castyt,
        "get_cast_infos",
        lambda: [
            SimpleNamespace(friendly_name="Cucinino", host="192.168.1.50"),
            SimpleNamespace(friendly_name="Salotto", host="192.168.1.60"),
        ],
    )
    monkeypatch.setattr(castyt, "CattDevice", Mock(return_value=Mock()))

    castyt._get_device()

    castyt.CattDevice.assert_called_once_with(ip_addr="192.168.1.50")


def test_get_device_raises_device_unavailable_when_no_cast_is_found(monkeypatch):
    monkeypatch.delenv("NEST_DEVICE_NAME", raising=False)
    monkeypatch.setattr(castyt, "DISCOVERY_RETRIES", 1)
    monkeypatch.setattr(castyt, "get_cast_infos", lambda: [])

    with pytest.raises(castyt.DeviceUnavailable):
        castyt._get_device()


def test_get_device_raises_device_unavailable(monkeypatch):
    monkeypatch.setenv("NEST_DEVICE_NAME", "Cucinino")
    monkeypatch.setattr(castyt, "DISCOVERY_RETRIES", 2)
    monkeypatch.setattr(castyt.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(castyt, "CattDevice", Mock(side_effect=CastError("nope")))

    with pytest.raises(castyt.DeviceUnavailable):
        castyt._get_device()


def test_try_play_locked_aborts_on_device_unavailable(monkeypatch):
    # A discovery/transport failure must not be mistaken for an unplayable track:
    # no index advance, no per-entry rediscovery, just abort.
    player = castyt.Player()
    player.query = "q"
    player.queue = [
        {"id": "a", "title": "A", "url": "u1"},
        {"id": "b", "title": "B", "url": "u2"},
    ]
    player.index = 0
    player._play_current_locked = Mock(side_effect=castyt.DeviceUnavailable("nope"))
    player._reset_cast_device_locked = Mock()
    player._grow_queue_locked = Mock()

    with pytest.raises(castyt.DeviceUnavailable):
        player._try_play_locked()

    assert player.index == 0
    player._reset_cast_device_locked.assert_not_called()
    player._grow_queue_locked.assert_not_called()


def test_new_media_status_swallows_device_unavailable():
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "u"
    player._play_next_locked = Mock(side_effect=castyt.DeviceUnavailable("nope"))

    player.new_media_status(
        SimpleNamespace(player_state="IDLE", idle_reason="FINISHED", content_id="u")
    )  # must not raise into the callback thread

    player._play_next_locked.assert_called_once()


def test_stop_clears_state_when_device_unavailable(monkeypatch):
    monkeypatch.setattr(castyt, "_get_device", Mock(side_effect=castyt.DeviceUnavailable("nope")))
    player = castyt.Player()
    player._announcing_content_id = "http://host/announce.mp3?t=abc"
    player.query = "q"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0

    player.stop()  # must not raise

    assert player.query is None
    assert player.queue == []
    assert player.index == -1
