import asyncio
from types import SimpleNamespace

import pytest

import motion


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


def test_debounce_seconds_defaults_to_30(monkeypatch):
    monkeypatch.delenv("MOTION_DEBOUNCE_SECONDS", raising=False)
    assert motion._debounce_seconds() == 30


def test_debounce_seconds_respects_env_as_int(monkeypatch):
    monkeypatch.setenv("MOTION_DEBOUNCE_SECONDS", "60")
    assert motion._debounce_seconds() == 60


def test_classification_timeout_defaults_to_10(monkeypatch):
    monkeypatch.delenv("MOTION_CLASSIFICATION_TIMEOUT_SECONDS", raising=False)
    assert motion._classification_timeout_seconds() == 10


def test_classification_timeout_respects_env_as_int(monkeypatch):
    monkeypatch.setenv("MOTION_CLASSIFICATION_TIMEOUT_SECONDS", "25")
    assert motion._classification_timeout_seconds() == 25


class _StopWatching(Exception):
    pass


def _motion_message():
    data = SimpleNamespace(SimpleItem=[SimpleNamespace(Name="IsMotion", Value="true")])
    return SimpleNamespace(
        Topic=SimpleNamespace(_value_1="tns1:VideoSource/MotionAlarm"),
        Message=SimpleNamespace(_value_1=SimpleNamespace(Data=data)),
    )


def _class_message(class_types):
    data = SimpleNamespace(SimpleItem=[SimpleNamespace(Name="ClassTypes", Value=class_types)])
    return SimpleNamespace(
        Topic=SimpleNamespace(_value_1="tns1:ObjectDetection/Object"),
        Message=SimpleNamespace(_value_1=SimpleNamespace(Data=data)),
    )


class _FakeService:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def PullMessages(self, params):
        messages = await self.queue.get()
        if messages is None:
            raise _StopWatching()
        return SimpleNamespace(NotificationMessage=messages)


class _FakeManager:
    def __init__(self, service):
        self._service = service

    def get_service(self):
        return self._service

    async def shutdown(self):
        pass


class _FakeCamera:
    def __init__(self, manager):
        self._manager = manager

    async def create_pullpoint_manager(self, interval, lost_cb):
        return self._manager

    async def close(self):
        pass


def _run(coro):
    return asyncio.run(coro)


async def _drive(monkeypatch, timeout, steps, alerts=1):
    """Run watch_motion against a fake ONVIF camera, feeding (delay, messages)
    steps, and return the categories reported to on_motion once `alerts` have
    been reported (or the drive times out)."""
    service = _FakeService()
    camera = _FakeCamera(_FakeManager(service))

    async def _fake_connect():
        return camera

    monkeypatch.setattr(motion, "_connect_camera", _fake_connect)
    monkeypatch.setattr(motion, "_classification_timeout_seconds", lambda: timeout)

    categories = []
    seen = asyncio.Event()

    async def on_motion(category):
        categories.append(category)
        if len(categories) >= alerts:
            seen.set()

    task = asyncio.create_task(motion.watch_motion(on_motion))
    await asyncio.sleep(0.01)  # let watch_motion subscribe

    for delay, messages in steps:
        if delay:
            await asyncio.sleep(delay)
        await service.queue.put(messages)

    try:
        await asyncio.wait_for(seen.wait(), timeout=timeout + 1)
    except asyncio.TimeoutError:
        pass

    await service.queue.put(None)
    with pytest.raises(_StopWatching):
        await task
    return categories


def test_classification_arriving_after_the_old_two_second_window_still_alerts(monkeypatch):
    # The old code slept a fixed 2s and read a snapshot, dropping anything later.
    categories = _run(
        _drive(
            monkeypatch,
            timeout=5,
            steps=[(0, [_motion_message()]), (2.2, [_class_message("Human")])],
        )
    )
    assert categories == ["person"]


def test_classification_arriving_before_the_alarm_is_still_used(monkeypatch):
    categories = _run(
        _drive(
            monkeypatch,
            timeout=1,
            steps=[(0, [_class_message("Human"), _motion_message()])],
        )
    )
    assert categories == ["person"]


def test_motion_without_classification_reports_unknown(monkeypatch):
    categories = _run(_drive(monkeypatch, timeout=0.2, steps=[(0, [_motion_message()])]))
    assert categories == ["unknown"]


def test_consumed_classification_is_not_reused_by_a_later_alarm(monkeypatch):
    # A classification that resolved one event must not seed the next one, or a
    # later unclassified alarm would falsely report the previous category.
    monkeypatch.setenv("MOTION_DEBOUNCE_SECONDS", "0")
    categories = _run(
        _drive(
            monkeypatch,
            timeout=5,
            steps=[
                (0, [_motion_message()]),
                (0.1, [_class_message("Human")]),
                (0.3, [_motion_message()]),
            ],
            alerts=2,
        )
    )
    assert categories == ["person", "unknown"]
