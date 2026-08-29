import tts


class _FakeGTTS:
    saved = []

    def __init__(self, text, lang):
        self.text = text
        self.lang = lang

    def save(self, path):
        _FakeGTTS.saved.append((self.text, self.lang, path))


def test_synthesize_passes_text_and_lang_and_returns_path(monkeypatch, tmp_path):
    _FakeGTTS.saved = []
    monkeypatch.setattr(tts, "gTTS", _FakeGTTS)
    path = tmp_path / "out.mp3"

    result = tts.synthesize("hello there", lang="en", path=path)

    assert result == path
    assert _FakeGTTS.saved == [("hello there", "en", str(path))]


def test_synthesize_defaults_to_default_path(monkeypatch):
    _FakeGTTS.saved = []
    monkeypatch.setattr(tts, "gTTS", _FakeGTTS)

    result = tts.synthesize("hi")

    assert result == tts.DEFAULT_PATH
    assert _FakeGTTS.saved[0][1] == "en"
