import os

import pytest

# bot.py reads these at import time; set defaults so importing it under test
# doesn't require a real .env file.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OWNER_USER_ID", "1")

import storage


@pytest.fixture(autouse=True)
def isolated_db(tmp_path_factory, monkeypatch):
    db_dir = tmp_path_factory.mktemp("cucinacast-db")
    monkeypatch.setattr(storage, "DB_PATH", db_dir / "test.db")
    storage.init_db()
