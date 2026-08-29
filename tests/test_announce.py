from unittest.mock import Mock

import pytest

import announce


def _make_handler():
    handler = object.__new__(announce._AnnounceHandler)
    handler.path = announce._URL_PATH
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.send_error = Mock()
    handler.end_headers = Mock()
    handler.wfile = Mock()
    return handler


def test_do_get_wrong_path_returns_404():
    handler = _make_handler()
    handler.path = "/something-else.mp3"

    handler.do_GET()

    handler.send_error.assert_called_once_with(404)
    handler.send_response.assert_not_called()


class _FakePath:
    def __init__(self, read_bytes):
        self._read_bytes = read_bytes

    def read_bytes(self):
        return self._read_bytes()


def test_do_get_missing_file_returns_404(monkeypatch):
    handler = _make_handler()

    def raise_oserror():
        raise OSError("no such file")

    monkeypatch.setattr(announce, "DEFAULT_PATH", _FakePath(raise_oserror))

    handler.do_GET()

    handler.send_error.assert_called_once_with(404)
    handler.send_response.assert_not_called()


def test_do_get_success_serves_file(monkeypatch):
    handler = _make_handler()
    monkeypatch.setattr(announce, "DEFAULT_PATH", _FakePath(lambda: b"mp3-bytes"))

    handler.do_GET()

    handler.send_response.assert_called_once_with(200)
    handler.send_header.assert_any_call("Content-Type", "audio/mpeg")
    handler.send_header.assert_any_call("Content-Length", "9")
    handler.end_headers.assert_called_once()
    handler.wfile.write.assert_called_once_with(b"mp3-bytes")


def test_get_lan_ip_uses_announce_host_env_when_set(monkeypatch):
    monkeypatch.setenv("ANNOUNCE_HOST", "10.0.0.5")
    assert announce._get_lan_ip() == "10.0.0.5"


def test_get_lan_ip_uses_socket_when_no_env(monkeypatch):
    monkeypatch.delenv("ANNOUNCE_HOST", raising=False)
    fake_socket = Mock()
    fake_socket.getsockname.return_value = ("192.168.1.20", 0)
    monkeypatch.setattr(announce.socket, "socket", lambda *a, **k: fake_socket)

    assert announce._get_lan_ip() == "192.168.1.20"
    fake_socket.close.assert_called_once()


def test_get_lan_ip_raises_runtime_error_on_oserror(monkeypatch):
    monkeypatch.delenv("ANNOUNCE_HOST", raising=False)
    fake_socket = Mock()
    fake_socket.connect.side_effect = OSError("network unreachable")
    monkeypatch.setattr(announce.socket, "socket", lambda *a, **k: fake_socket)

    with pytest.raises(RuntimeError):
        announce._get_lan_ip()
    fake_socket.close.assert_called_once()


def test_announce_port_defaults(monkeypatch):
    monkeypatch.delenv("ANNOUNCE_PORT", raising=False)
    assert announce._announce_port() == 8765


def test_announce_port_respects_env(monkeypatch):
    monkeypatch.setenv("ANNOUNCE_PORT", "9999")
    assert announce._announce_port() == 9999
