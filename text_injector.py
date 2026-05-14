"""
Text Injector — inserts transcribed text into the currently active text field.

Preserves the user's clipboard: saves before pasting, restores after.

Tracks the target window (the one with focus when recording started) so
text always lands in the right place, even if the user clicks the tray icon
to stop recording (which steals focus).

Sets a module-level flag (injection_active) during keyboard simulation
so the hotkey listener can ignore simulated keypresses.
"""

import ctypes
import logging
import time
import threading

import pyperclip
from pynput.keyboard import Controller, Key

import config

logger = logging.getLogger(__name__)

_keyboard = Controller()
_inject_lock = threading.Lock()
_user32 = ctypes.windll.user32

# Module-level flag: set during keyboard simulation so hotkey listener
# can ignore simulated keypresses that would otherwise phantom-trigger.
injection_active = threading.Event()


class TextInjector:

    def __init__(self):
        self._interim_char_count = 0
        self._target_hwnd = None

    def capture_target_window(self):
        """Save the currently focused window so we can restore it before pasting."""
        hwnd = _user32.GetForegroundWindow()
        if hwnd:
            self._target_hwnd = hwnd
            logger.debug(f"Target window captured: {hwnd}")

    def _restore_target_window(self):
        """Restore focus to the window that was active when recording started."""
        if self._target_hwnd:
            try:
                if _user32.IsWindow(self._target_hwnd):
                    _user32.SetForegroundWindow(self._target_hwnd)
                    time.sleep(0.15)
                    logger.debug(f"Focus restored to window: {self._target_hwnd}")
            except Exception as e:
                logger.debug(f"Could not restore focus: {e}")

    def inject(self, text: str):
        if not text or not text.strip():
            return
        text = text.strip()
        if config.INJECT_TRAILING_SPACE:
            text += " "

        with _inject_lock:
            self._restore_target_window()
            if self._interim_char_count > 0:
                self._send_backspaces(self._interim_char_count)
                self._interim_char_count = 0
            self._paste(text)

    def inject_interim(self, text: str):
        if not text or not text.strip():
            return
        text = text.strip()

        with _inject_lock:
            if self._interim_char_count > 0:
                self._send_backspaces(self._interim_char_count)
            self._paste(text)
            self._interim_char_count = len(text)

    def clear_interim(self):
        with _inject_lock:
            if self._interim_char_count > 0:
                self._send_backspaces(self._interim_char_count)
                self._interim_char_count = 0

    @property
    def has_interim(self) -> bool:
        return self._interim_char_count > 0

    def reset(self):
        self._interim_char_count = 0

    # ─── Internal ────────────────────────────────────────────────

    def _paste(self, text: str):
        try:
            # Save the user's clipboard
            saved_clipboard = None
            if config.CLIPBOARD_RESTORE:
                try:
                    saved_clipboard = pyperclip.paste()
                except Exception:
                    pass

            pyperclip.copy(text)
            time.sleep(0.03)

            injection_active.set()
            _keyboard.press(Key.ctrl)
            _keyboard.press("v")
            _keyboard.release("v")
            _keyboard.release(Key.ctrl)
            time.sleep(0.05)
            injection_active.clear()

            # Restore the user's clipboard
            if config.CLIPBOARD_RESTORE and saved_clipboard is not None:
                time.sleep(0.05)
                try:
                    pyperclip.copy(saved_clipboard)
                except Exception:
                    pass

        except Exception as e:
            injection_active.clear()
            logger.error(f"Paste failed: {e}")

    def _send_backspaces(self, count: int):
        try:
            injection_active.set()
            for _ in range(count):
                _keyboard.press(Key.backspace)
                _keyboard.release(Key.backspace)
            time.sleep(0.03)
            injection_active.clear()
        except Exception as e:
            injection_active.clear()
            logger.error(f"Backspace failed: {e}")
            self._interim_char_count = 0
