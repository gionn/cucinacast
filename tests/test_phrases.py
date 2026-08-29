from datetime import datetime

import phrases


def test_tts_lang_defaults_to_english(monkeypatch):
    monkeypatch.delenv("TTS_LANG", raising=False)
    assert phrases.tts_lang() == "en"


def test_tts_lang_respects_env_var(monkeypatch):
    monkeypatch.setenv("TTS_LANG", "it")
    assert phrases.tts_lang() == "it"


def test_announcement_text_known_category_known_lang(monkeypatch):
    monkeypatch.setenv("TTS_LANG", "it")
    assert phrases.announcement_text("person") == "Qualcuno è alla porta — credo sia una persona."


def test_announcement_text_unknown_category_falls_back_to_unknown_phrase(monkeypatch):
    monkeypatch.setenv("TTS_LANG", "en")
    assert phrases.announcement_text("spaceship") == "Someone is at the door."


def test_announcement_text_unconfigured_lang_falls_back_to_english(monkeypatch):
    monkeypatch.setenv("TTS_LANG", "fr")
    assert phrases.announcement_text("animal") == "Someone is at the door — I think it's an animal."


def _at_hour(hour):
    return datetime(2024, 1, 1, hour, 0)


def test_in_quiet_hours_default_window_matches_late_night(monkeypatch):
    monkeypatch.delenv("QUIET_HOURS_START", raising=False)
    monkeypatch.delenv("QUIET_HOURS_END", raising=False)
    assert phrases.in_quiet_hours(_at_hour(23)) is True


def test_in_quiet_hours_default_window_matches_early_morning(monkeypatch):
    monkeypatch.delenv("QUIET_HOURS_START", raising=False)
    monkeypatch.delenv("QUIET_HOURS_END", raising=False)
    assert phrases.in_quiet_hours(_at_hour(7)) is True


def test_in_quiet_hours_default_window_excludes_daytime(monkeypatch):
    monkeypatch.delenv("QUIET_HOURS_START", raising=False)
    monkeypatch.delenv("QUIET_HOURS_END", raising=False)
    assert phrases.in_quiet_hours(_at_hour(12)) is False


def test_in_quiet_hours_start_boundary_is_inclusive(monkeypatch):
    monkeypatch.delenv("QUIET_HOURS_START", raising=False)
    monkeypatch.delenv("QUIET_HOURS_END", raising=False)
    assert phrases.in_quiet_hours(_at_hour(22)) is True


def test_in_quiet_hours_end_boundary_is_exclusive(monkeypatch):
    monkeypatch.delenv("QUIET_HOURS_START", raising=False)
    monkeypatch.delenv("QUIET_HOURS_END", raising=False)
    assert phrases.in_quiet_hours(_at_hour(8)) is False


def test_in_quiet_hours_same_day_window(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_START", "1")
    monkeypatch.setenv("QUIET_HOURS_END", "5")
    assert phrases.in_quiet_hours(_at_hour(3)) is True
    assert phrases.in_quiet_hours(_at_hour(6)) is False
    assert phrases.in_quiet_hours(_at_hour(0)) is False


def test_in_quiet_hours_defaults_to_now_when_not_given(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_START", "0")
    monkeypatch.setenv("QUIET_HOURS_END", "23")

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _at_hour(10)

    monkeypatch.setattr(phrases, "datetime", _FixedDatetime)

    assert phrases.in_quiet_hours() is True


def test_in_quiet_hours_invalid_start_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_START", "not-a-number")
    monkeypatch.delenv("QUIET_HOURS_END", raising=False)
    assert phrases.in_quiet_hours(_at_hour(23)) is True
    assert phrases.in_quiet_hours(_at_hour(12)) is False


def test_in_quiet_hours_out_of_range_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS_END", "24")
    monkeypatch.delenv("QUIET_HOURS_START", raising=False)
    assert phrases.in_quiet_hours(_at_hour(7)) is True
    assert phrases.in_quiet_hours(_at_hour(8)) is False
