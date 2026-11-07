import ipaddress
import os
import socket

import pytest
import zeroconf

# bot.py reads these at import time; set defaults so importing it under test
# doesn't require a real .env file.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OWNER_USER_ID", "1")

import storage

_LOCAL_NAMES = {"", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


class _OfflineZeroconf:
    """Stand-in for zeroconf.Zeroconf, which opens multicast sockets as soon as
    it is constructed."""

    offline = True

    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


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
def offline_zeroconf():
    """Keep discovery stubs from falling back to a real Zeroconf instance."""
    real_zeroconf = zeroconf.Zeroconf
    zeroconf.Zeroconf = _OfflineZeroconf
    try:
        yield
    finally:
        zeroconf.Zeroconf = real_zeroconf


@pytest.fixture(scope="session", autouse=True)
def no_network():
    """Reject anything beyond loopback, so a test that forgot to stub a call
    fails on every machine instead of passing on one that happens to be online."""
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    real_sendto = socket.socket.sendto
    real_sendmsg = socket.socket.sendmsg

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

    def guarded_sendto(sock, data, *args):
        # Datagrams skip connect(), so they need their own guard.
        address = args[-1] if args else None
        if isinstance(address, tuple) and not _is_local(address[0]):
            refuse(address)
        return real_sendto(sock, data, *args)

    def guarded_sendmsg(sock, buffers, *args, **kwargs):
        address = kwargs.get("address")
        if address is None and len(args) > 2:
            address = args[2]
        if isinstance(address, tuple) and not _is_local(address[0]):
            refuse(address)
        return real_sendmsg(sock, buffers, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect(real_connect)
    socket.socket.connect_ex = guarded_connect(real_connect_ex)
    socket.create_connection = guarded_create_connection
    socket.socket.sendto = guarded_sendto
    socket.socket.sendmsg = guarded_sendmsg
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
        socket.create_connection = real_create_connection
        socket.socket.sendto = real_sendto
        socket.socket.sendmsg = real_sendmsg


@pytest.fixture(autouse=True)
def isolated_db(tmp_path_factory, monkeypatch):
    db_dir = tmp_path_factory.mktemp("cucinacast-db")
    monkeypatch.setattr(storage, "DB_PATH", db_dir / "test.db")
    storage.init_db()
