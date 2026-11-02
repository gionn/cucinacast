import ipaddress
import os
import socket

import pytest

# bot.py reads these at import time; set defaults so importing it under test
# doesn't require a real .env file.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OWNER_USER_ID", "1")

import storage

_LOCAL_NAMES = {"", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _is_local(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode()
    if host in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(scope="session", autouse=True)
def no_network():
    """Reject anything beyond loopback, so a test that forgot to stub a call
    fails on every machine instead of passing on one that happens to be online."""
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def refuse(target):
        raise OSError(f"Tests must not reach {target!r}; stub the call instead")

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_local(host):
            refuse(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(original):
        def connect(sock, address):
            if isinstance(address, tuple) and not _is_local(address[0]):
                refuse(address)
            return original(sock, address)

        return connect

    def guarded_create_connection(address, *args, **kwargs):
        if not _is_local(address[0]):
            refuse(address)
        return real_create_connection(address, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect(real_connect)
    socket.socket.connect_ex = guarded_connect(real_connect_ex)
    socket.create_connection = guarded_create_connection
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
        socket.create_connection = real_create_connection


@pytest.fixture(autouse=True)
def isolated_db(tmp_path_factory, monkeypatch):
    db_dir = tmp_path_factory.mktemp("cucinacast-db")
    monkeypatch.setattr(storage, "DB_PATH", db_dir / "test.db")
    storage.init_db()
