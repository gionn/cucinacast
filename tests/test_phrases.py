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
