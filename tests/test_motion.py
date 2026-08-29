import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import motion


def _run(coro):
    return asyncio.run(coro)


def _fake_camera(stream_uri="rtsp://cam/stream"):
    media = Mock()
    media.GetProfiles = AsyncMock(
        return_value=[SimpleNamespace(token="profile-sub")]  # noqa: S106
    )
    media.GetStreamUri = AsyncMock(return_value=SimpleNamespace(Uri=stream_uri))
    camera = Mock()
    camera.create_media_service = AsyncMock(return_value=media)
    return camera


def _fake_proc(returncode=0, stderr=b"", times_out=False):
    proc = Mock()
    if times_out:
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    else:
        proc.communicate = AsyncMock(return_value=(b"", stderr))
    proc.returncode = returncode
    proc.kill = Mock()
    proc.wait = AsyncMock()
    return proc


def _patch_capture_deps(monkeypatch, tmp_path, proc):
    monkeypatch.setenv("ONVIF_USER", "admin")
    monkeypatch.setenv("ONVIF_PASS", "p@ss:word")
    monkeypatch.setattr(motion, "_connect_camera", AsyncMock(return_value=_fake_camera()))
    monkeypatch.setattr(motion, "_clip_dir_path", lambda: str(tmp_path))
    create_subprocess_mock = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_mock)
    return create_subprocess_mock


def test_describe_object_matches_person_keywords():
    assert motion.describe_object("Human") == "person"
    assert motion.describe_object("face detected") == "person"


def test_describe_object_matches_animal_keywords():
    assert motion.describe_object("Animal") == "animal"
    assert motion.describe_object("pet") == "animal"


def test_describe_object_matches_vehicle_keywords():
    assert motion.describe_object("Vehicle") == "vehicle"
    assert motion.describe_object("car") == "vehicle"


def test_describe_object_is_case_insensitive():
    assert motion.describe_object("PERSON") == "person"


def test_describe_object_falls_back_to_unknown():
    assert motion.describe_object(None) == "unknown"
    assert motion.describe_object("") == "unknown"
    assert motion.describe_object("something else") == "unknown"


def test_motion_detection_enabled_requires_both_user_and_pass(monkeypatch):
    monkeypatch.delenv("ONVIF_USER", raising=False)
    monkeypatch.delenv("ONVIF_PASS", raising=False)
    assert motion.motion_detection_enabled() is False

    monkeypatch.setenv("ONVIF_USER", "admin")
    assert motion.motion_detection_enabled() is False

    monkeypatch.setenv("ONVIF_PASS", "secret")
    assert motion.motion_detection_enabled() is True


def test_onvif_host_defaults_to_none(monkeypatch):
    monkeypatch.delenv("ONVIF_HOST", raising=False)
    assert motion._onvif_host() is None


def test_onvif_host_respects_env(monkeypatch):
    monkeypatch.setenv("ONVIF_HOST", "192.168.1.50")
    assert motion._onvif_host() == "192.168.1.50"


def test_onvif_port_defaults_to_80(monkeypatch):
    monkeypatch.delenv("ONVIF_PORT", raising=False)
    assert motion._onvif_port() == 80


def test_onvif_port_respects_env_as_int(monkeypatch):
    monkeypatch.setenv("ONVIF_PORT", "8080")
    assert motion._onvif_port() == 8080


def test_onvif_user_and_pass_respect_env(monkeypatch):
    monkeypatch.setenv("ONVIF_USER", "admin")
    monkeypatch.setenv("ONVIF_PASS", "secret")
    assert motion._onvif_user() == "admin"
    assert motion._onvif_pass() == "secret"


def test_capture_clip_returns_path_and_encodes_credentials(monkeypatch, tmp_path):
    proc = _fake_proc(returncode=0)
    create_subprocess_mock = _patch_capture_deps(monkeypatch, tmp_path, proc)

    path = _run(motion.capture_clip(duration_seconds=1))

    assert path.parent == tmp_path
    assert path.exists()
    args = create_subprocess_mock.await_args.args
    stream_url = args[args.index("-i") + 1]
    assert "admin:p@ss:word@" not in stream_url
    assert "p%40ss%3Aword" in stream_url


def test_capture_clip_deletes_file_and_redacts_password_on_failure(monkeypatch, tmp_path):
    proc = _fake_proc(
        returncode=1, stderr=b"could not connect to rtsp://admin:p@ss:word@cam/stream"
    )
    _patch_capture_deps(monkeypatch, tmp_path, proc)

    with pytest.raises(RuntimeError) as exc_info:
        _run(motion.capture_clip(duration_seconds=1))

    assert "p@ss:word" not in str(exc_info.value)
    assert list(tmp_path.iterdir()) == []


def test_capture_clip_kills_process_and_deletes_file_on_timeout(monkeypatch, tmp_path):
    proc = _fake_proc(times_out=True)
    _patch_capture_deps(monkeypatch, tmp_path, proc)

    with pytest.raises(RuntimeError, match="timed out"):
        _run(motion.capture_clip(duration_seconds=1))

    proc.kill.assert_called_once()
    assert list(tmp_path.iterdir()) == []
