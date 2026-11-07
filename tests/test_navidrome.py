import hashlib
import json
import urllib.parse
from unittest.mock import Mock

import pytest

import navidrome


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("NAVIDROME_URL", "http://192.168.1.10:4533/")
    monkeypatch.setenv("NAVIDROME_USER", "gionn")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "sesame")
    monkeypatch.setenv("NAVIDROME_TIMEOUT", "1.5")
    monkeypatch.delenv("NAVIDROME_FORMAT", raising=False)


def _query(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_enabled_requires_url_user_and_password(configured, monkeypatch):
    assert navidrome.enabled() is True

    for variable in ("NAVIDROME_URL", "NAVIDROME_USER", "NAVIDROME_PASSWORD"):
        monkeypatch.delenv(variable)
        assert navidrome.enabled() is False
        monkeypatch.setenv(variable, "x")


def test_stream_url_uses_token_auth_and_mp3(configured):
    url = navidrome.stream_url("song-1")

    assert url.startswith("http://192.168.1.10:4533/rest/stream.view?")
    query = _query(url)
    assert query["id"] == ["song-1"]
    assert query["format"] == ["mp3"]
    assert query["u"] == ["gionn"]
    assert query["v"] == ["1.16.1"]
    assert query["c"] == ["cucinacast"]
    assert query["f"] == ["json"]
    expected = hashlib.md5(("sesame" + query["s"][0]).encode(), usedforsecurity=False).hexdigest()
    assert query["t"] == [expected]


def test_stream_url_keeps_the_raw_format(configured, monkeypatch):
    monkeypatch.setenv("NAVIDROME_FORMAT", "raw")

    query = _query(navidrome.stream_url("song-1"))

    assert query["format"] == ["raw"]


def test_cover_url_targets_the_album(configured):
    url = navidrome.cover_url("album-1")

    assert "/rest/getCoverArt.view?" in url
    query = _query(url)
    assert query["id"] == ["album-1"]
    assert query["size"] == ["300"]


def test_search_returns_album_and_song_matches(configured, monkeypatch):
    calls = []

    def fake_request(method, params):
        calls.append((method, params))
        return {
            "searchResult3": {
                "album": [{"id": "a1", "name": "Album One", "artist": "Artist"}],
                "song": [
                    {
                        "id": "s1",
                        "title": "Song One",
                        "artist": "Artist",
                        "album": "Album One",
                        "albumId": "a1",
                        "suffix": "mp3",
                    },
                    {
                        "id": "s2",
                        "title": "Song Two",
                        "artist": "Other Artist",
                        "album": "Album Two",
                        "albumId": "a2",
                        "suffix": "flac",
                    },
                ],
            }
        }

    monkeypatch.setattr(navidrome, "_request", fake_request)

    result = navidrome.search("query")

    assert calls[0][0] == "search3"
    assert calls[0][1]["query"] == "query"
    assert calls[0][1]["albumCount"] == navidrome.ALBUM_LIMIT
    assert calls[0][1]["songCount"] == navidrome.SONG_LIMIT
    assert [album["id"] for album in result["albums"]] == ["a1", "a2"]
    assert result["albums"][1] == {
        "id": "a2",
        "name": "Album Two",
        "artist": "Other Artist",
    }
    assert [song["id"] for song in result["songs"]] == ["s1", "s2"]


def test_search_accepts_single_objects_instead_of_lists(configured, monkeypatch):
    monkeypatch.setattr(
        navidrome,
        "_request",
        lambda method, params: {
            "searchResult3": {
                "album": {"id": "a1", "name": "Album One", "artist": "Artist"},
                "song": {
                    "id": "s1",
                    "title": "Song One",
                    "artist": "Artist",
                    "album": "Album One",
                    "albumId": "a1",
                },
            }
        },
    )

    result = navidrome.search("query")

    assert [album["id"] for album in result["albums"]] == ["a1"]
    assert [song["id"] for song in result["songs"]] == ["s1"]


def test_search_treats_an_empty_response_as_no_match(configured, monkeypatch):
    monkeypatch.setattr(navidrome, "_request", lambda method, params: {})

    assert navidrome.search("query") == {"albums": [], "songs": []}


def test_album_entries_build_direct_stream_entries(configured, monkeypatch):
    monkeypatch.setattr(
        navidrome,
        "_request",
        lambda method, params: {
            "album": {
                "id": "a1",
                "name": "Album One",
                "artist": "Artist",
                "song": [
                    {"id": "s1", "title": "First", "artist": "Artist", "suffix": "mp3"},
                    {"id": "s2", "title": "Second", "artist": "Artist", "suffix": "flac"},
                ],
            }
        },
    )

    album = navidrome.album_entries("a1")

    assert album["id"] == "a1"
    assert album["name"] == "Album One"
    assert album["artist"] == "Artist"
    first = album["entries"][0]
    assert first["id"] == "s1"
    assert first["title"] == "First — Artist"
    assert first["url"].startswith("http://192.168.1.10:4533/rest/stream.view?")
    assert first["content_type"] == "audio/mpeg"
    assert "/rest/getCoverArt.view?" in first["thumb"]
    assert [entry["id"] for entry in album["entries"]] == ["s1", "s2"]


def test_album_entries_raises_when_the_album_has_no_songs(configured, monkeypatch):
    monkeypatch.setattr(
        navidrome,
        "_request",
        lambda method, params: {"album": {"id": "a1", "name": "Empty", "song": []}},
    )

    with pytest.raises(navidrome.NavidromeUnavailable):
        navidrome.album_entries("a1")


def test_song_entry_uses_the_song_suffix_for_raw_streams(configured, monkeypatch):
    monkeypatch.setenv("NAVIDROME_FORMAT", "raw")
    monkeypatch.setattr(
        navidrome,
        "_request",
        lambda method, params: {
            "song": {
                "id": "s1",
                "title": "Song",
                "artist": "Artist",
                "albumId": "a1",
                "suffix": "flac",
            }
        },
    )

    entry = navidrome.song_entry("s1")

    assert entry["content_type"] == "audio/flac"
    assert _query(entry["url"])["format"] == ["raw"]


def test_song_entry_raises_when_song_is_missing(configured, monkeypatch):
    monkeypatch.setattr(navidrome, "_request", lambda method, params: {})

    with pytest.raises(navidrome.NavidromeUnavailable):
        navidrome.song_entry("missing")


def test_request_raises_when_not_configured(monkeypatch):
    for variable in ("NAVIDROME_URL", "NAVIDROME_USER", "NAVIDROME_PASSWORD"):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(navidrome.NavidromeUnavailable):
        navidrome.search("query")


def test_request_wraps_network_errors(configured, monkeypatch):
    monkeypatch.setattr(
        navidrome.urllib.request, "urlopen", Mock(side_effect=OSError("connection refused"))
    )

    with pytest.raises(navidrome.NavidromeUnavailable, match="connection refused"):
        navidrome.search("query")


def test_request_surfaces_api_errors(configured, monkeypatch):
    monkeypatch.setattr(
        navidrome.urllib.request,
        "urlopen",
        Mock(
            return_value=_Response(
                {
                    "subsonic-response": {
                        "status": "failed",
                        "error": {"code": 40, "message": "Wrong username or password"},
                    }
                }
            )
        ),
    )

    with pytest.raises(navidrome.NavidromeUnavailable, match="Wrong username or password"):
        navidrome.search("query")


def test_request_rejects_unexpected_response_shapes(configured, monkeypatch):
    monkeypatch.setattr(
        navidrome.urllib.request, "urlopen", Mock(return_value=_Response(["not", "an", "object"]))
    )

    with pytest.raises(navidrome.NavidromeUnavailable):
        navidrome.search("query")

    monkeypatch.setattr(
        navidrome.urllib.request,
        "urlopen",
        Mock(return_value=_Response({"subsonic-response": "not an object"})),
    )

    with pytest.raises(navidrome.NavidromeUnavailable):
        navidrome.search("query")


def test_request_hits_the_expected_endpoint(configured, monkeypatch):
    opened = []

    def fake_urlopen(url, timeout):
        opened.append((url, timeout))
        return _Response({"subsonic-response": {"status": "ok"}})

    monkeypatch.setattr(navidrome.urllib.request, "urlopen", fake_urlopen)

    navidrome.search("daft punk")

    url, timeout = opened[0]
    assert url.startswith("http://192.168.1.10:4533/rest/search3.view?")
    assert _query(url)["query"] == ["daft punk"]
    assert timeout == 1.5


def test_timeout_falls_back_to_default_on_bad_value(configured, monkeypatch):
    monkeypatch.setenv("NAVIDROME_TIMEOUT", "not-a-number")

    assert navidrome._timeout() == navidrome.DEFAULT_TIMEOUT


def test_search_tracks_are_normalised(configured, monkeypatch):
    monkeypatch.setattr(
        navidrome,
        "_request",
        lambda method, params: {
            "searchResult3": {
                "song": {"id": "s1", "title": "Song", "artist": "Artist", "albumId": "a1"}
            }
        },
    )

    result = navidrome.search("query")

    assert result["songs"] == [
        {
            "id": "s1",
            "title": "Song",
            "artist": "Artist",
            "album_id": "a1",
            "album": "",
        }
    ]
