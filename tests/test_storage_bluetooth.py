import pytest

import storage_bluetooth


def test_normalize_mac_uppercases_and_adds_colons():
    assert storage_bluetooth.normalize_mac("aa:bb:cc:dd:ee:ff") == "AA:BB:CC:DD:EE:FF"


def test_normalize_mac_tolerates_dashes_and_lowercase():
    assert storage_bluetooth.normalize_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"


def test_normalize_mac_tolerates_no_separators():
    assert storage_bluetooth.normalize_mac("aabbccddeeff") == "AA:BB:CC:DD:EE:FF"


def test_normalize_mac_rejects_short_address():
    with pytest.raises(ValueError):
        storage_bluetooth.normalize_mac("aa:bb:cc")


def test_normalize_mac_rejects_non_hex():
    with pytest.raises(ValueError):
        storage_bluetooth.normalize_mac("zz:zz:zz:zz:zz:zz")


def test_add_and_list_devices():
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta")
    devices = storage_bluetooth.list_devices()
    assert len(devices) == 1
    assert devices[0]["mac"] == "AA:BB:CC:DD:EE:FF"
    assert devices[0]["nickname"] == "Marta"
    assert devices[0]["home"] is False
    assert devices[0]["miss_count"] == 0
    assert devices[0]["last_seen"] is None


def test_list_devices_sorted_by_nickname():
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Zoe")
    storage_bluetooth.add_device("11:22:33:44:55:66", "Bob")
    nicknames = [d["nickname"] for d in storage_bluetooth.list_devices()]
    assert nicknames == ["Bob", "Zoe"]


def test_set_device_state():
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta")
    storage_bluetooth.set_device_state("AA:BB:CC:DD:EE:FF", True, 3, 123.0)
    device = storage_bluetooth.list_devices()[0]
    assert device["home"] is True
    assert device["miss_count"] == 3
    assert device["last_seen"] == 123.0


def test_remove_device():
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta")
    storage_bluetooth.remove_device("AA:BB:CC:DD:EE:FF")
    assert storage_bluetooth.list_devices() == []


def test_add_device_normalizes_mac():
    storage_bluetooth.add_device("aa-bb-cc-dd-ee-ff", "Marta")
    assert storage_bluetooth.list_devices()[0]["mac"] == "AA:BB:CC:DD:EE:FF"


def test_remove_device_normalizes_mac():
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta")
    storage_bluetooth.remove_device("aa-bb-cc-dd-ee-ff")
    assert storage_bluetooth.list_devices() == []


def test_set_device_state_normalizes_mac():
    storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta")
    storage_bluetooth.set_device_state("aa-bb-cc-dd-ee-ff", True, 1, 1.0)
    device = storage_bluetooth.list_devices()[0]
    assert device["home"] is True
    assert device["miss_count"] == 1


def test_add_device_rejects_invalid_mac():
    with pytest.raises(ValueError):
        storage_bluetooth.add_device("not-a-mac", "Marta")


def test_add_duplicate_mac_returns_false():
    assert storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Marta") is True
    assert storage_bluetooth.add_device("AA:BB:CC:DD:EE:FF", "Someone Else") is False
