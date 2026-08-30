"""Persistent sqlite storage for Bluetooth presence devices, no Telegram/TTS/
casting dependency."""

import re
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "cucinacast.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS devices "
        "(mac TEXT PRIMARY KEY, nickname TEXT NOT NULL, added_at REAL NOT NULL, "
        "home INTEGER NOT NULL DEFAULT 0, miss_count INTEGER NOT NULL DEFAULT 0, "
        "last_seen REAL)"
    )
    return conn


def normalize_mac(mac):
    """Normalize a MAC address to uppercase with colons (AA:BB:CC:DD:EE:FF),
    tolerating any separator/case from user input."""
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(cleaned) != 12:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2)).upper()


def add_device(mac, nickname):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO devices (mac, nickname, added_at) VALUES (?, ?, ?)",
            (mac, nickname, time.time()),
        )


def remove_device(mac):
    with _connect() as conn:
        conn.execute("DELETE FROM devices WHERE mac = ?", (mac,))


def list_devices():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT mac, nickname, home, miss_count, last_seen FROM devices ORDER BY nickname"
        ).fetchall()
    return [
        {
            "mac": row[0],
            "nickname": row[1],
            "home": bool(row[2]),
            "miss_count": row[3],
            "last_seen": row[4],
        }
        for row in rows
    ]


def set_device_state(mac, home, miss_count, last_seen):
    with _connect() as conn:
        conn.execute(
            "UPDATE devices SET home = ?, miss_count = ?, last_seen = ? WHERE mac = ?",
            (int(home), miss_count, last_seen, mac),
        )
