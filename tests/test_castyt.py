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


_FINISHED_STATUS = SimpleNamespace(player_state="IDLE", idle_reason="FINISHED")


def test_new_media_status_ignores_non_finished_status():
    player = castyt.Player()
    player._play_next_locked = Mock()

    player.new_media_status(SimpleNamespace(player_state="BUFFERING", idle_reason=None))

    player._play_next_locked.assert_not_called()


def test_new_media_status_advances_queue_when_not_announcing():
    player = castyt.Player()
    player._announcing = False
    player._play_next_locked = Mock()

    player.new_media_status(_FINISHED_STATUS)

    player._play_next_locked.assert_called_once()


def test_new_media_status_resumes_after_announcement(monkeypatch):
    player = castyt.Player()
    player._announcing = True
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._interrupted_at_seconds = 12.3
    player._play_current_locked = Mock()
    player._play_next_locked = Mock()

    player.new_media_status(_FINISHED_STATUS)

    player._play_current_locked.assert_called_once_with(current_time=12.3)
    assert player._announcing is False
    player._play_next_locked.assert_not_called()


def test_new_media_status_resets_device_on_cast_error_resuming():
    player = castyt.Player()
    player._announcing = True
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._play_current_locked = Mock(side_effect=CastError("fail"))
    player._reset_cast_device_locked = Mock()
    player._try_play_locked = Mock()

    player.new_media_status(_FINISHED_STATUS)

    player._reset_cast_device_locked.assert_called_once()
    assert player.index == 1
    player._try_play_locked.assert_called_once()


def test_new_media_status_falls_back_without_reset_on_generic_error_resuming():
    player = castyt.Player()
    player._announcing = True
    player.queue = [{"id": "a", "title": "A", "url": "u"}]
    player.index = 0
    player._play_current_locked = Mock(side_effect=RuntimeError("boom"))
    player._reset_cast_device_locked = Mock()
    player._try_play_locked = Mock()

    player.new_media_status(_FINISHED_STATUS)

    player._reset_cast_device_locked.assert_not_called()
    assert player.index == 1
    player._try_play_locked.assert_called_once()


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
    devices[0]._cast.disconnect.assert_called_once_with(blocking=False)


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
