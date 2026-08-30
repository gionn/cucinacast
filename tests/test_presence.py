import asyncio
from unittest.mock import AsyncMock

import pytest

import presence
import storage_bluetooth


def _add_home_device(mac="AA:BB:CC:DD:EE:FF", nickname="Marta"):
    storage_bluetooth.add_device(mac, nickname)
    storage_bluetooth.set_device_state(mac, True, 0, 0.0)


def _run(coro):
    return asyncio.run(coro)


def test_bluetooth_available_true_when_adapter_present(monkeypatch):
    monkeypatch.setattr(
        presence, "_run_checked", lambda cmd, timeout=15: "Devices:\n\thci0\tDC:A6:32:95:50:20"
    )
    assert presence.bluetooth_available() is True


def test_bluetooth_available_false_when_adapter_missing(monkeypatch):
    monkeypatch.setattr(presence, "_run_checked", lambda cmd, timeout=15: "")
    assert presence.bluetooth_available() is False


def test_bluetooth_available_false_on_hcitool_error(monkeypatch):
    def boom(cmd, timeout=15):
        raise RuntimeError("no adapter")

    monkeypatch.setattr(presence, "_run_checked", boom)
    assert presence.bluetooth_available() is False


def test_bluetooth_available_warns_with_setup_advice_on_error(monkeypatch, caplog):
    def boom(cmd, timeout=15):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(presence, "_run_checked", boom)
    with caplog.at_level("WARNING", logger="presence"):
        assert presence.bluetooth_available() is False
    assert any("bluetooth" in r.message and "usermod" in r.message for r in caplog.records)


def test_strip_ansi_removes_color_codes_and_control_bytes():
    raw = "[\x01\x1b[0;92m\x02NEW\x01\x1b[0m\x02] Device B0:4A:B4:B0:A0:27 moto g84"
    assert presence._strip_ansi(raw) == "[NEW] Device B0:4A:B4:B0:A0:27 moto g84"


def test_discover_devices_parses_ansi_colored_output(monkeypatch):
    raw = (
        "Discovery started\n"
        "[\x01\x1b[0;92m\x02NEW\x01\x1b[0m\x02] Device 49:F1:E2:E9:BE:0A 49-F1-E2-E9-BE-0A\n"
        "[\x01\x1b[0;92m\x02NEW\x01\x1b[0m\x02] Device B0:4A:B4:B0:A0:27 moto g84\n"
    )
    monkeypatch.setattr(presence, "_run_async", AsyncMock(return_value=raw))

    devices = _run(presence.discover_devices())

    assert devices == [
        {"mac": "49:F1:E2:E9:BE:0A", "name": ""},
        {"mac": "B0:4A:B4:B0:A0:27", "name": "moto g84"},
    ]


def test_discover_devices_uses_late_reported_name(monkeypatch):
    raw = (
        "Discovery started\n"
        "[NEW] Device B0:4A:B4:B0:A0:27 B0-4A-B4-B0-A0-27\n"
        "[CHG] Device B0:4A:B4:B0:A0:27 Name: moto g84\n"
        "[CHG] Device B0:4A:B4:B0:A0:27 RSSI: -70\n"
    )
    monkeypatch.setattr(presence, "_run_async", AsyncMock(return_value=raw))

    devices = _run(presence.discover_devices())

    assert devices == [{"mac": "B0:4A:B4:B0:A0:27", "name": "moto g84"}]


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


def test_probe_runs_hcitool_without_sudo(monkeypatch):
    captured = {}

    def capture(cmd, timeout=15):
        captured["cmd"] = cmd
        return "Pixel 9"

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


class _FakeStdin:
    def __init__(self):
        self.data = []

    def write(self, chunk):
        self.data.append(chunk)

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeStdout:
    def __init__(self, lines, hang=False):
        self._lines = list(lines)
        self._hang = hang

    def at_eof(self):
        return not self._lines

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._hang:
            await asyncio.sleep(3600)
        return b""


class _FakeProc:
    def __init__(self, lines, hang=False):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines, hang=hang)
        self.killed = False

    async def wait(self):
        return 0

    def kill(self):
        self.killed = True


def _patch_proc(monkeypatch, lines, hang=False):
    proc = _FakeProc(lines, hang=hang)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    return proc


def _run_pairing(monkeypatch, lines, hang=False, on_prompt=None):
    proc = _patch_proc(monkeypatch, lines, hang=hang)

    async def _confirm(passkey):
        return "yes"

    result = _run(presence._pair_interactive("AA:BB:CC:DD:EE:FF", on_prompt or _confirm))
    return proc, result


def test_pair_success_via_patch(monkeypatch):
    proc, (paired, outcome) = _run_pairing(monkeypatch, [b"Pairing successful\n"])
    assert paired is True
    assert outcome == "success"


def test_pair_failure_on_bluetoothctl_exit(monkeypatch):
    proc, (paired, outcome) = _run_pairing(monkeypatch, [b"Attempting to pair\n"])
    assert paired is False
    assert outcome == "bluetoothctl exited before pairing finished"


def test_pair_failure_reported_by_bluetoothctl(monkeypatch):
    proc, (paired, outcome) = _run_pairing(monkeypatch, [b"Failed to pair\n"])
    assert paired is False
    assert outcome == "bluetoothctl reported failure"


def test_pair_aborted_by_user(monkeypatch):
    async def _abort(passkey):
        return None

    proc, (paired, outcome) = _run_pairing(
        monkeypatch,
        [b"Attempting to pair\n", b"Confirm passkey 123456\n"],
        on_prompt=_abort,
    )
    assert paired is False
    assert outcome == "aborted by user"


def test_pair_confirms_passkey_then_succeeds(monkeypatch):
    replies = []

    async def on_prompt(passkey):
        replies.append(passkey)
        return "yes"

    proc, (paired, outcome) = _run_pairing(
        monkeypatch,
        [b"Confirm passkey 123456\n", b"Pairing successful\n"],
        on_prompt=on_prompt,
    )
    assert paired is True
    assert outcome == "success"
    assert replies == ["123456"]


def test_pair_times_out_when_no_output(monkeypatch):
    monkeypatch.setattr(presence, "PAIR_TIMEOUT_SECONDS", 0.1)
    proc, (paired, outcome) = _run_pairing(monkeypatch, [], hang=True)
    assert paired is False
    assert outcome == "timed out"
    assert b"quit\n" in proc.stdin.data


def test_pair_times_out_waiting_for_user_confirmation(monkeypatch):
    monkeypatch.setattr(presence, "PASSKEY_CONFIRM_TIMEOUT_SECONDS", 0.1)

    async def _never_replies(passkey):
        await asyncio.sleep(3600)

    proc, (paired, outcome) = _run_pairing(
        monkeypatch, [b"Confirm passkey 061796 (yes/no):\n"], on_prompt=_never_replies
    )
    assert paired is False
    assert outcome == "timed out waiting for user confirmation"
