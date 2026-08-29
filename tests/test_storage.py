import storage


def test_recent_ids_empty_for_unknown_query():
    assert storage.recent_ids("nonexistent") == set()


def test_record_play_tracked_in_recent_ids():
    storage.record_play("daft punk", "abc123")
    assert storage.recent_ids("daft punk") == {"abc123"}


def test_recent_ids_scoped_per_query():
    storage.record_play("daft punk", "abc123")
    storage.record_play("coldplay", "def456")
    assert storage.recent_ids("daft punk") == {"abc123"}
    assert storage.recent_ids("coldplay") == {"def456"}


def test_record_play_prunes_beyond_history_limit(monkeypatch):
    monkeypatch.setattr(storage, "HISTORY_LIMIT", 3)
    for i in range(5):
        storage.record_play("daft punk", f"id{i}")
    assert storage.recent_ids("daft punk") == {"id2", "id3", "id4"}


def test_record_play_prunes_even_when_same_id_replayed(monkeypatch):
    monkeypatch.setattr(storage, "HISTORY_LIMIT", 3)
    for _ in range(10):
        storage.record_play("daft punk", "same-id")
    with storage._connect() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) FROM plays WHERE query = ?", ("daft punk",)
        ).fetchone()[0]
    assert row_count <= 3


def test_record_play_breaks_played_at_ties_by_insertion_order(monkeypatch):
    monkeypatch.setattr(storage, "HISTORY_LIMIT", 3)
    monkeypatch.setattr(storage.time, "time", lambda: 1000.0)
    for i in range(5):
        storage.record_play("daft punk", f"id{i}")
    assert storage.recent_ids("daft punk") == {"id2", "id3", "id4"}


def test_cached_pool_miss_when_absent():
    assert storage.cached_pool("daft punk", min_count=5) is None


def test_store_pool_then_cached_pool_hit():
    entries = [{"id": "a", "title": "Song A", "view_count": 100}]
    storage.store_pool("daft punk", entries)
    assert storage.cached_pool("daft punk", min_count=1) == entries


def test_cached_pool_miss_when_fewer_rows_than_requested():
    storage.store_pool("daft punk", [{"id": "a", "title": "Song A", "view_count": 100}])
    assert storage.cached_pool("daft punk", min_count=2) is None


def test_cached_pool_miss_when_stale(monkeypatch):
    storage.store_pool("daft punk", [{"id": "a", "title": "Song A", "view_count": 100}])
    monkeypatch.setattr(storage, "CACHE_TTL_SECONDS", -1)
    assert storage.cached_pool("daft punk", min_count=1) is None


def test_store_pool_replaces_previous_entries():
    storage.store_pool("daft punk", [{"id": "a", "title": "Song A", "view_count": 100}])
    storage.store_pool("daft punk", [{"id": "b", "title": "Song B", "view_count": 200}])
    cached = storage.cached_pool("daft punk", min_count=1)
    assert [e["id"] for e in cached] == ["b"]
