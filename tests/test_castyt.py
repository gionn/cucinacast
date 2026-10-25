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


def _media_status(content_id=None, session_id=None, state="PLAYING", idle_reason=None):
    return SimpleNamespace(
        player_state=state,
        idle_reason=idle_reason,
        content_id=content_id,
        media_session_id=session_id,
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
    player._announcing = False
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "u"
    player._play_next_locked = Mock()

    player.new_media_status(
        SimpleNamespace(player_state="IDLE", idle_reason="FINISHED", content_id="u")
    )

    player._play_next_locked.assert_called_once()


def test_new_media_status_ignores_finished_without_any_ids():
    # If the status proves no ownership, treat it as not ours.
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "u"
    player._play_next_locked = Mock()

    player.new_media_status(_FINISHED_STATUS)  # content_id and session id both None

    player._play_next_locked.assert_not_called()


def test_new_media_status_advances_on_session_id_match():
    # The session id alone proves ownership when the content id is absent.
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "u"
    player._current_session_id = 42
    player._play_next_locked = Mock()

    player.new_media_status(
        SimpleNamespace(
            player_state="IDLE",
            idle_reason="FINISHED",
            content_id=None,
            media_session_id=42,
        )
    )

    player._play_next_locked.assert_called_once()


def test_captures_track_session_id_from_device_status():
    # The session id read right after play_media_url can be a stale one (catt's
    # block_until_active returns on the previous session), so it is taken from the
    # status once our own content is current instead.
    player = castyt.Player()
    player.query = "daft punk"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "u"
    player._play_next_locked = Mock()

    player.new_media_status(_media_status(content_id="u", session_id=7))

    assert player._current_session_id == 7

    player.new_media_status(
        _media_status(content_id="u", session_id=7, state="IDLE", idle_reason="FINISHED")
    )

    player._play_next_locked.assert_called_once()


def test_does_not_capture_session_id_for_other_content():
    player = castyt.Player()
    player._current_content_id = "u"

    player.new_media_status(_media_status(content_id="other", session_id=7))

    assert player._current_session_id is None


def test_captures_announcement_session_id_from_device_status():
    # Regression: the id read straight after play_media_url was the previous
    # announcement's, so the announcement's own FINISHED was rejected as someone
    # else's and the resume was dropped.
    announcement_url = "http://host/announce.mp3"
    player = castyt.Player()
    player._announcing = True
    player._announcing_content_id = announcement_url
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._interrupted_at_seconds = 12.3
    player._play_current_locked = Mock()

    player.new_media_status(_media_status(content_id=announcement_url, session_id=9))

    assert player._announcing_session_id == 9

    player.new_media_status(
        _media_status(
            content_id=announcement_url, session_id=9, state="IDLE", idle_reason="FINISHED"
        )
    )

    player._play_current_locked.assert_called_once_with(current_time=12.3)


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


def test_new_media_status_ignores_when_session_ids_differ_even_if_content_matches():
    # Announcements reuse one URL, so a conflicting session id must win over a
    # matching content id.
    player = castyt.Player()
    player._announcing = True
    player._announcing_content_id = "http://host/cucinacast_announce.mp3"
    player._announcing_session_id = 5
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._play_current_locked = Mock()
    player._try_play_locked = Mock()

    player.new_media_status(
        SimpleNamespace(
            player_state="IDLE",
            idle_reason="FINISHED",
            content_id="http://host/cucinacast_announce.mp3",
            media_session_id=9,
        )
    )

    player._play_current_locked.assert_not_called()
    player._try_play_locked.assert_not_called()


def test_ignores_terminal_status_of_replaced_announcement_when_capturing_session():
    # Announcing a second time resets the captured id while the first
    # announcement's terminal status is still in flight. Claiming that stale id
    # makes the new announcement's FINISHED fail the match and drops the resume.
    announcement_url = "http://host/announce.mp3"
    player = castyt.Player()
    player._announcing = True
    player._announcing_content_id = announcement_url
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._interrupted_at_seconds = 12.3
    player._play_current_locked = Mock()

    player.new_media_status(
        _media_status(
            content_id=announcement_url, session_id=1, state="IDLE", idle_reason="CANCELLED"
        )
    )
    player.new_media_status(_media_status(content_id=announcement_url, session_id=2))

    assert player._announcing_session_id == 2

    player.new_media_status(
        _media_status(
            content_id=announcement_url, session_id=2, state="IDLE", idle_reason="FINISHED"
        )
    )

    player._play_current_locked.assert_called_once_with(current_time=12.3)


def test_new_media_status_resumes_after_announcement(monkeypatch):
    player = castyt.Player()
    player._announcing = True
    player._announcing_content_id = "http://host/announce.mp3"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._interrupted_at_seconds = 12.3
    player._play_current_locked = Mock()
    player._play_next_locked = Mock()

    player.new_media_status(
        SimpleNamespace(
            player_state="IDLE",
            idle_reason="FINISHED",
            content_id="http://host/announce.mp3",
        )
    )

    player._play_current_locked.assert_called_once_with(current_time=12.3)
    assert player._announcing is False
    assert player._announcing_content_id is None
    player._play_next_locked.assert_not_called()


def test_new_media_status_drops_resume_when_announcement_replaced():
    # The user replaced the announcement with their own media, so the interrupted
    # track must not resume over it.
    player = castyt.Player()
    player._announcing = True
    player._announcing_content_id = "http://host/announce.mp3"
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
    assert player._announcing is False
    assert player._announcing_content_id is None


def test_new_media_status_resets_device_on_cast_error_resuming():
    player = castyt.Player()
    player._announcing = True
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
    player._announcing = True
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


def test_ensure_device_injects_live_zeroconf(monkeypatch):
    fake_device = _FakeDevice()
    sentinel_zconf = object()
    monkeypatch.setattr(castyt, "_get_device", lambda: fake_device)
    monkeypatch.setattr(castyt.zeroconf, "Zeroconf", lambda: sentinel_zconf)
    player = castyt.Player()

    player._ensure_device()

    assert player._device is fake_device
    assert fake_device._cast.socket_client.zconf is sentinel_zconf
    fake_device._cast.media_controller.register_status_listener.assert_called_once_with(player)


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
    assert player._announcing is True
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

    assert player._announcing is False
    assert player._announcing_content_id is None


def test_announce_clears_state_and_reraises_on_device_unavailable(monkeypatch):
    monkeypatch.setattr(castyt, "_get_device", Mock(side_effect=castyt.DeviceUnavailable("nope")))
    player = castyt.Player()
    player._announcing = True
    player._announcing_content_id = "old"
    player._announcing_session_id = 1

    with pytest.raises(castyt.DeviceUnavailable):
        player.announce("http://host/audio.mp3")

    assert player._announcing is False
    assert player._announcing_content_id is None
    assert player._announcing_session_id is None


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
    assert player._current_session_id is None


def test_play_current_locked_leaves_session_id_for_the_status_callback(monkeypatch):
    # The session id read straight after play_media_url can be the previous
    # session's, so _play_current_locked leaves it to _capture_session_id.
    device = _FakeDevice()
    device._cast.media_controller.status.media_session_id = 7
    monkeypatch.setattr(castyt, "_get_device", lambda: device)
    monkeypatch.setattr(castyt, "StreamInfo", _FakeStreamInfo)
    player = castyt.Player()
    player.query = "q"
    player.queue = [{"id": "a", "title": "A", "url": "u1"}]
    player.index = 0

    player._play_current_locked()

    assert player._current_session_id is None


def test_play_current_locked_clears_ownership_ids_on_failure(monkeypatch):
    monkeypatch.setattr(castyt, "_get_device", Mock(side_effect=castyt.DeviceUnavailable("nope")))
    player = castyt.Player()
    player.query = "q"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._current_content_id = "stale"
    player._current_session_id = 5

    with pytest.raises(castyt.DeviceUnavailable):
        player._play_current_locked()

    assert player._current_content_id is None
    assert player._current_session_id is None


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
    player._announcing = True
    player.query = "q"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0

    player.stop()

    assert player.query is None
    assert player.queue == []
    assert player.index == -1
    assert player._announcing is False
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
    player._announcing = True
    player.query = "q"
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0

    player.stop()  # must not raise

    assert player.query is None
    assert player.queue == []
    assert player.index == -1
    assert player._announcing is False
