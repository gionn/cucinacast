"""Persistent sqlite storage for play history and search-result caching, no
Telegram/casting dependency."""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "cucinacast.db"

HISTORY_LIMIT = 20
CACHE_TTL_SECONDS = 7 * 24 * 3600


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS plays (query TEXT, video_id TEXT, played_at REAL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS search_cache "
        "(query TEXT, video_id TEXT, title TEXT, view_count INTEGER, fetched_at REAL)"
    )
    return conn


def record_play(query, video_id):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO plays (query, video_id, played_at) VALUES (?, ?, ?)",
            (query, video_id, time.time()),
        )
        conn.execute(
            "DELETE FROM plays WHERE query = ? AND rowid NOT IN ("
            "SELECT rowid FROM plays WHERE query = ? ORDER BY played_at DESC LIMIT ?"
            ")",
            (query, query, HISTORY_LIMIT),
        )


def recent_ids(query):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT video_id FROM plays WHERE query = ? ORDER BY played_at DESC LIMIT ?",
            (query, HISTORY_LIMIT),
        ).fetchall()
    return {row[0] for row in rows}


def cached_pool(query, min_count):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT video_id, title, view_count, fetched_at FROM search_cache "
            "WHERE query = ? ORDER BY rowid",
            (query,),
        ).fetchall()
    if len(rows) < min_count:
        return None
    if time.time() - max(row[3] for row in rows) > CACHE_TTL_SECONDS:
        return None
    return [{"id": row[0], "title": row[1], "view_count": row[2]} for row in rows]


def store_pool(query, entries):
    fetched_at = time.time()
    with _connect() as conn:
        conn.execute("DELETE FROM search_cache WHERE query = ?", (query,))
        conn.executemany(
            "INSERT INTO search_cache (query, video_id, title, view_count, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(query, e["id"], e["title"], e.get("view_count"), fetched_at) for e in entries],
        )
