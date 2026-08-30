import asyncio

import pytest

import presence
import storage_bluetooth


def _add_home_device(mac="AA:BB:CC:DD:EE:FF", nickname="Marta"):
    storage_bluetooth.add_device(mac, nickname)
    storage_bluetooth.set_device_state(mac, True, 0, 0.0)


def _run(coro):
    return asyncio.run(coro)


def test_bluetooth_available_true_when_adapter_present(monkeypatch):
    monkeypatch.setattr(presence, "_run_checked", lambda cmd, timeout=15: "hci0 DC:A6:32:95:50:20")
    assert presence.bluetooth_available() is True


def test_bluetooth_available_false_when_adapter_missing(monkeypatch):
    monkeypatch.setattr(presence, "_run_checked", lambda cmd, timeout=15: "")
    assert presence.bluetooth_available() is False


def test_bluetooth_available_false_on_hcitool_error(monkeypatch):
    def boom(cmd, timeout=15):
        raise RuntimeError("no adapter")

    monkeypatch.setattr(presence, "_run_checked", boom)
    assert presence.bluetooth_available() is False


def test_apply_miss_increments_count():
    home, miss_count, flipped = presence._apply_miss({"home": True, "miss_count": 0})
    assert (home, miss_count, flipped) == (True, 1, False)


def test_apply_miss_flips_to_away_at_threshold():
    home, miss_count, flipped = presence._apply_miss({"home": True, "miss_count": 2})
    assert (home, miss_count, flipped) == (False, 3, True)


def test_apply_miss_keeps_away_device_away():
    home, miss_count, flipped = presence._apply_miss({"home": False, "miss_count": 0})
    assert (home, miss_count, flipped) == (False, 1, False)


def test_check_presence_returns_empty_without_devices():
    assert _run(presence.check_presence()) == []


def test_check_presence_sighting_flips_home(monkeypatch):
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta")
    monkeypatch.setattr(presence, "_probe", lambda mac: True)

    transitions = _run(presence.check_presence())

    assert transitions == [{"nickname": "Marta", "mac": "AA:BB:CC:DD:EE:FF", "home": True}]
    device = storage_bluetooth.list_devices()[0]
    assert device["home"] is True
    assert device["miss_count"] == 0


def test_check_presence_away_after_three_misses(monkeypatch):
    _add_home_device()
    monkeypatch.setattr(presence, "_probe", lambda mac: False)

    assert _run(presence.check_presence()) == []
    assert _run(presence.check_presence()) == []
    transitions = _run(presence.check_presence())

    assert transitions == [{"nickname": "Marta", "mac": "AA:BB:CC:DD:EE:FF", "home": False}]
    device = storage_bluetooth.list_devices()[0]
    assert device["home"] is False
    assert device["miss_count"] == 3


def test_check_presence_two_misses_stays_home(monkeypatch):
    _add_home_device()
    monkeypatch.setattr(presence, "_probe", lambda mac: False)

    assert _run(presence.check_presence()) == []
    assert _run(presence.check_presence()) == []

    device = storage_bluetooth.list_devices()[0]
    assert device["home"] is True
    assert device["miss_count"] == 2


def test_check_presence_sighting_resets_miss_count(monkeypatch):
    _add_home_device()
    storage_bluetooth.set_device_state("AA:BB:CC:DD:EE:FF", True, 2, 0.0)
    monkeypatch.setattr(presence, "_probe", lambda mac: True)

    transitions = _run(presence.check_presence())

    assert transitions == []
    device = storage_bluetooth.list_devices()[0]
    assert device["home"] is True
    assert device["miss_count"] == 0


def test_probe_returns_false_on_timeout(monkeypatch):
    def boom(cmd, timeout=15):
        raise TimeoutError

    monkeypatch.setattr(presence, "_run_checked", boom)
    assert presence._probe("AA:BB:CC:DD:EE:FF") is False


def test_probe_returns_true_for_named_device(monkeypatch):
    monkeypatch.setattr(presence, "_run_checked", lambda cmd, timeout=15: "Pixel 9")
    assert presence._probe("AA:BB:CC:DD:EE:FF") is True


def test_probe_prepends_sudo_by_default(monkeypatch):
    captured = {}

    def capture(cmd, timeout=15):
        captured["cmd"] = cmd
        return "Pixel 9"

    monkeypatch.setattr(presence, "_run_checked", capture)
    presence._probe("AA:BB:CC:DD:EE:FF")
    assert captured["cmd"] == ("sudo", "hcitool", "name", "AA:BB:CC:DD:EE:FF")


def test_probe_drops_sudo_with_presence_no_sudo(monkeypatch):
    captured = {}

    def capture(cmd, timeout=15):
        captured["cmd"] = cmd
        return "Pixel 9"

    monkeypatch.setattr(presence, "SUDO_PREFIX", ())
    monkeypatch.setattr(presence, "_run_checked", capture)
    presence._probe("AA:BB:CC:DD:EE:FF")
    assert captured["cmd"] == ("hcitool", "name", "AA:BB:CC:DD:EE:FF")


def test_run_forever_skips_when_no_devices(monkeypatch):
    monkeypatch.setattr(presence.storage_bluetooth, "list_devices", lambda: [])
    called = []

    async def should_not_run():
        called.append(1)
        raise AssertionError("check_presence should not run with no devices")

    monkeypatch.setattr(presence, "check_presence", should_not_run)

    async def scenario():
        task = asyncio.create_task(presence.run_forever(lambda t: None, poll_interval_seconds=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())
    assert called == []


def test_run_forever_restarts_after_failure(monkeypatch):
    _add_home_device()
    calls = {"count": 0}

    async def flaky():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(presence, "check_presence", flaky)
    transitions = []

    async def scenario():
        task = asyncio.create_task(
            presence.run_forever(transitions.append, poll_interval_seconds=0.01)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())
    assert calls["count"] >= 2
