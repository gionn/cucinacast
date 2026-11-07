"""Optional Navidrome backend: search albums and songs, stream them directly."""

import hashlib
import json
import logging
import os
import secrets
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

API_VERSION = "1.16.1"
CLIENT_NAME = "cucinacast"
ALBUM_LIMIT = 5
SONG_LIMIT = 5
DEFAULT_TIMEOUT = 3.0

_MIME_TYPES = {
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "wma": "audio/x-ms-wma",
}


class NavidromeUnavailable(Exception):
    """Navidrome is not configured, unreachable, or returned an error."""


def _config():
    return (
        (os.environ.get("NAVIDROME_URL") or "").strip(),
        (os.environ.get("NAVIDROME_USER") or "").strip(),
        os.environ.get("NAVIDROME_PASSWORD") or "",
    )


def enabled():
    return all(_config())


def _timeout():
    value = os.environ.get("NAVIDROME_TIMEOUT")
    if not value:
        return DEFAULT_TIMEOUT
    try:
        return float(value)
    except ValueError:
        logger.warning("Ignoring invalid NAVIDROME_TIMEOUT=%r", value)
        return DEFAULT_TIMEOUT


def _stream_format():
    return os.environ.get("NAVIDROME_FORMAT") or "mp3"


def _auth_params():
    _, user, password = _config()
    salt = secrets.token_hex(4)
    # Subsonic mandates MD5 for token authentication; it is not a security choice.
    token = hashlib.md5((password + salt).encode("utf-8"), usedforsecurity=False).hexdigest()
    return {
        "u": user,
        "t": token,
        "s": salt,
        "v": API_VERSION,
        "c": CLIENT_NAME,
        "f": "json",
    }


def _api_url(method, params):
    url, _, _ = _config()
    if not url:
        raise NavidromeUnavailable("Navidrome is not configured")
    query = {**_auth_params(), **params}
    return f"{url.rstrip('/')}/rest/{method}.view?{urllib.parse.urlencode(query)}"


def _request(method, params):
    if not enabled():
        raise NavidromeUnavailable("Navidrome is not configured")
    url = _api_url(method, params)
    try:
        with urllib.request.urlopen(url, timeout=_timeout()) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("unexpected response")
    except Exception as exc:
        raise NavidromeUnavailable(f"Navidrome {method} request failed: {exc}") from exc
    body = payload.get("subsonic-response")
    if not isinstance(body, dict):
        raise NavidromeUnavailable(f"Navidrome {method} returned an unexpected response")
    if body.get("status") != "ok":
        message = (body.get("error") or {}).get("message") or f"Navidrome {method} failed"
        raise NavidromeUnavailable(message)
    return body


def _as_list(value):
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def _album(item):
    return {
        "id": item["id"],
        "name": item.get("name") or item.get("title") or "",
        "artist": item.get("artist") or "",
    }


def _song(item):
    return {
        "id": item["id"],
        "title": item.get("title") or "",
        "artist": item.get("artist") or "",
        "album": item.get("album") or "",
        "album_id": item.get("albumId") or "",
    }


def _with_song_albums(albums, songs, limit):
    seen = {album["id"] for album in albums}
    merged = list(albums)
    for song in songs:
        album_id = song["album_id"]
        if not album_id or not song["album"] or album_id in seen:
            continue
        seen.add(album_id)
        merged.append({"id": album_id, "name": song["album"], "artist": song["artist"]})
        if len(merged) >= limit:
            break
    return merged[:limit]


def search(query, album_limit=ALBUM_LIMIT, song_limit=SONG_LIMIT):
    body = _request(
        "search3",
        {
            "query": query,
            "albumCount": album_limit,
            "albumOffset": 0,
            "songCount": song_limit,
            "songOffset": 0,
            "artistCount": 0,
        },
    )
    result = body.get("searchResult3") or {}
    albums = [_album(item) for item in _as_list(result.get("album"))]
    songs = [_song(item) for item in _as_list(result.get("song"))]
    return {"albums": _with_song_albums(albums, songs, album_limit), "songs": songs}


def _track_title(song, album):
    title = song.get("title") or "Unknown"
    artist = song.get("artist") or album.get("artist")
    if not artist or artist == title:
        return title
    return f"{title} — {artist}"


def _content_type(song):
    if _stream_format() != "raw":
        return "audio/mpeg"
    suffix = (song.get("suffix") or "").lower()
    return _MIME_TYPES.get(suffix, "audio/mpeg")


def _entry(song, album):
    album_id = song.get("albumId") or album.get("id")
    return {
        "id": song["id"],
        "title": _track_title(song, album),
        "url": stream_url(song["id"]),
        "content_type": _content_type(song),
        "thumb": cover_url(album_id) if album_id else None,
    }


def album_entries(album_id):
    body = _request("getAlbum", {"id": album_id})
    album = body.get("album") or {}
    songs = _as_list(album.get("song"))
    if not songs:
        raise NavidromeUnavailable("The album has no songs")
    return {
        "id": album.get("id") or album_id,
        "name": album.get("name") or "",
        "artist": album.get("artist") or "",
        "entries": [_entry(song, album) for song in songs],
    }


def song_entry(song_id):
    body = _request("getSong", {"id": song_id})
    song = body.get("song") or {}
    if not song:
        raise NavidromeUnavailable("The song was not found")
    return _entry(song, {})


def stream_url(song_id):
    return _api_url("stream", {"id": song_id, "format": _stream_format()})


def cover_url(album_id):
    return _api_url("getCoverArt", {"id": album_id, "size": 300})
