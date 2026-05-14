"""
Settings persistence — saves user preferences to a JSON file.

Stored next to the executable so settings follow the app.
Falls back to defaults from config.py if the file doesn't exist.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings.json"
)

_DEFAULTS = {
    "model": "base.en",
    "hotkey_mode": "toggle",       # "toggle" or "hold"
    "sound_feedback": True,
}


def _load_raw() -> dict:
    try:
        if os.path.exists(_SETTINGS_FILE):
            with open(_SETTINGS_FILE, "r", encoding="utf-8-sig") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read settings: {e}")
    return {}


def load() -> dict:
    """Load settings, filling in defaults for any missing keys."""
    raw = _load_raw()
    merged = {**_DEFAULTS, **raw}
    return merged


def save(settings: dict):
    """Save settings to disk."""
    try:
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        logger.info(f"Settings saved: {settings}")
    except Exception as e:
        logger.warning(f"Failed to save settings: {e}")


def get(key: str, default=None):
    """Get a single setting value."""
    s = load()
    return s.get(key, default if default is not None else _DEFAULTS.get(key))


def put(key: str, value):
    """Update a single setting and save."""
    s = load()
    s[key] = value
    save(s)
