"""
Sound feedback — short audio cues for dictation state changes.

Uses sounddevice (already a dependency) to play generated tones.
No external sound files needed.

  start:     ascending two-tone  (gentle "boop-beep")
  stop:      descending tone     (soft "boop")
  cancel:    low single tone     (brief "bonk")
  complete:  bright chime        (quick "ding")
"""

import logging
import threading

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

# Sample rate for tones (standard, not the recording rate)
_TONE_SR = 44100
_enabled = True


def set_enabled(enabled: bool):
    global _enabled
    _enabled = enabled


def _generate_tone(frequency: float, duration: float, volume: float = 0.3) -> np.ndarray:
    """Generate a smooth sine wave with fade in/out."""
    t = np.linspace(0, duration, int(_TONE_SR * duration), dtype=np.float32)
    tone = np.sin(2 * np.pi * frequency * t) * volume

    # Smooth fade in/out to avoid clicks (10ms each)
    fade_samples = int(_TONE_SR * 0.01)
    if fade_samples > 0 and len(tone) > fade_samples * 2:
        tone[:fade_samples] *= np.linspace(0, 1, fade_samples)
        tone[-fade_samples:] *= np.linspace(1, 0, fade_samples)

    return tone


def _play_async(audio: np.ndarray):
    """Play audio in a background thread so it never blocks."""
    def _play():
        try:
            sd.play(audio, _TONE_SR, blocking=True)
        except Exception as e:
            logger.debug(f"Sound playback failed: {e}")

    threading.Thread(target=_play, daemon=True).start()


def play_start():
    """Ascending two-tone: recording started."""
    if not _enabled:
        return
    t1 = _generate_tone(600, 0.08, volume=0.2)
    gap = np.zeros(int(_TONE_SR * 0.03), dtype=np.float32)
    t2 = _generate_tone(900, 0.08, volume=0.2)
    _play_async(np.concatenate([t1, gap, t2]))


def play_stop():
    """Descending tone: recording stopped, transcribing."""
    if not _enabled:
        return
    t1 = _generate_tone(700, 0.08, volume=0.2)
    gap = np.zeros(int(_TONE_SR * 0.03), dtype=np.float32)
    t2 = _generate_tone(450, 0.10, volume=0.2)
    _play_async(np.concatenate([t1, gap, t2]))


def play_cancel():
    """Low single tone: recording cancelled."""
    if not _enabled:
        return
    _play_async(_generate_tone(300, 0.15, volume=0.15))


def play_complete():
    """Bright chime: transcription done, text injected."""
    if not _enabled:
        return
    _play_async(_generate_tone(1200, 0.06, volume=0.15))
