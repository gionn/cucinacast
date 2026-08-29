"""Localized wording for motion-triggered doorbell announcements."""

import os

TTS_LANG = os.environ.get("TTS_LANG", "en")

_ANNOUNCEMENTS = {
    "en": {
        "person": "Someone is at the door — I think it's a person.",
        "animal": "Someone is at the door — I think it's an animal.",
        "vehicle": "Someone is at the door — I think it's a vehicle.",
        "unknown": "Someone is at the door.",
    },
    "it": {
        "person": "Qualcuno è alla porta — credo sia una persona.",
        "animal": "Qualcuno è alla porta — credo sia un animale.",
        "vehicle": "Qualcuno è alla porta — credo sia un veicolo.",
        "unknown": "Qualcuno è alla porta.",
    },
}


def announcement_text(category):
    """Return the announcement sentence for a motion category
    ("person"/"animal"/"vehicle"/"unknown") in TTS_LANG, falling back to English
    wording for unconfigured languages."""
    phrases = _ANNOUNCEMENTS.get(TTS_LANG, _ANNOUNCEMENTS["en"])
    return phrases.get(category, phrases["unknown"])
