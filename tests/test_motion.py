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
