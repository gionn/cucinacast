"""Bundled sound effects for motion-triggered alerts, no TTS/casting dependency."""

from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

_SOUNDS = {
    "person": ASSETS_DIR / "person.mp3",
    "animal": ASSETS_DIR / "animal.mp3",
    "vehicle": ASSETS_DIR / "vehicle.mp3",
}


def sound_path(category):
    """Return the bundled sound effect for a motion category
    ("person"/"animal"/"vehicle"), or None when the category has no sound
    (e.g. "unknown"). Callers skip playback on None."""
    return _SOUNDS.get(category)
