"""Text-to-speech synthesis, no casting/serving dependency."""
import sys
import tempfile
from pathlib import Path

from gtts import gTTS

DEFAULT_PATH = Path(tempfile.gettempdir()) / "cucinacast_announce.mp3"


def synthesize(text, lang="en", path=DEFAULT_PATH):
    """Synthesize text to speech and save it as an MP3 at path, returning path."""
    gTTS(text=text, lang=lang).save(str(path))
    return path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <text to speak> [output path]")
        sys.exit(1)
    text = sys.argv[1]
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PATH
    synthesize(text, path=out_path)
    print(f"Wrote {out_path}")
