"""
Hotkey Manager — global hotkey for dictation control.

Uses pynput Listener with manual key tracking instead of GlobalHotKeys,
which gets confused by simulated keypresses from the text injector.

Supports two modes:
  Toggle: Ctrl+Shift+Space → start, Ctrl+Shift+Space → stop
  Hold:   Hold Ctrl+Shift+Space → start, release → stop

Additional hotkeys:
  Escape → cancel recording (discard without transcribing)
"""

import logging
import time

from pynput import keyboard

import config
from text_injector import injection_active

logger = logging.getLogger(__name__)

DEBOUNCE_S = 0.3

MODE_TOGGLE = "toggle"
MODE_HOLD = "hold"


class HotkeyManager:
    def __init__(self):
        self._listener: keyboard.Listener | None = None
        self.on_toggle_dictation = None
        self.on_cancel_dictation = None
        self.on_stop_dictation = None    # For hold mode release
        self._pressed = set()
        self._last_trigger = 0.0
        self._hotkey_active = False      # Track if hotkey combo is held
        self._mode = MODE_TOGGLE

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str):
        if value in (MODE_TOGGLE, MODE_HOLD):
            self._mode = value
            logger.info(f"Hotkey mode: {value}")

    def start(self):
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        self._listener.start()
        logger.info(f"Hotkey: {config.HOTKEY_TOGGLE_DICTATION} → {self._mode} mode")
        logger.info("Hotkey: Escape → cancel recording")

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key):
        # Ignore simulated keypresses from text injection
        if injection_active.is_set():
            return

        normalized = self._normalize(key)
        self._pressed.add(normalized)

        # Check Escape for cancel
        if normalized == "escape":
            if self.on_cancel_dictation:
                self.on_cancel_dictation()
            return

        self._check_hotkey_press()

    def _on_release(self, key):
        normalized = self._normalize(key)
        self._pressed.discard(normalized)

        # Hold mode: release any part of the hotkey combo → stop
        if self._mode == MODE_HOLD and self._hotkey_active:
            if normalized in ("ctrl", "shift", "space"):
                self._hotkey_active = False
                logger.debug("Hotkey released: stop dictation (hold mode)")
                if self.on_stop_dictation:
                    self.on_stop_dictation()

    def _normalize(self, key):
        """Normalize key to a comparable value."""
        if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            return "ctrl"
        if key in (keyboard.Key.shift_l, keyboard.Key.shift_r):
            return "shift"
        if key == keyboard.Key.space:
            return "space"
        if key == keyboard.Key.esc:
            return "escape"
        return key

    def _check_hotkey_press(self):
        if {"ctrl", "shift", "space"}.issubset(self._pressed):
            now = time.time()
            if now - self._last_trigger < DEBOUNCE_S:
                return
            self._last_trigger = now

            if self._mode == MODE_TOGGLE:
                logger.debug("Hotkey: toggle dictation")
                if self.on_toggle_dictation:
                    self.on_toggle_dictation()
            elif self._mode == MODE_HOLD:
                if not self._hotkey_active:
                    self._hotkey_active = True
                    logger.debug("Hotkey held: start dictation (hold mode)")
                    if self.on_toggle_dictation:
                        self.on_toggle_dictation()
