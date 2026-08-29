"""Localized wording for motion-triggered doorbell announcements."""

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

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


def tts_lang():
    return os.environ.get("TTS_LANG", "en")


def _quiet_hour(env_var, default):
    value = os.environ.get(env_var)
    if value is None:
        return default
    try:
        hour = int(value)
    except ValueError:
        hour = None
    if hour is None or not 0 <= hour <= 23:
        logger.warning(
            "%s=%r is not an integer 0-23, falling back to default %d", env_var, value, default
        )
        return default
    return hour


def in_quiet_hours(now=None):
    """Return True if `now` (default: current local time) falls within the
    QUIET_HOURS_START/QUIET_HOURS_END window (local-time hours, 0-23), during which
    motion-triggered announcements are suppressed. Handles the overnight wraparound
    (e.g. 22 -> 8)."""
    start = _quiet_hour("QUIET_HOURS_START", 22)
    end = _quiet_hour("QUIET_HOURS_END", 8)
    hour = (now or datetime.now()).hour
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def announcement_text(category):
    """Return the announcement sentence for a motion category
    ("person"/"animal"/"vehicle"/"unknown") in TTS_LANG, falling back to English
    wording for unconfigured languages."""
    phrases = _ANNOUNCEMENTS.get(tts_lang(), _ANNOUNCEMENTS["en"])
    return phrases.get(category, phrases["unknown"])
