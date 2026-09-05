import os

import pytest

# bot.py reads these at import time; set defaults so importing it under test
# doesn't require a real .env file.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OWNER_USER_ID", "1")

import storage


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(storage, "DB_PATH", db_path)
