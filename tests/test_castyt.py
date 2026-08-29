import pytest

import castyt


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
