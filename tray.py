"""
System Tray — the primary interface for the dictation app.

Four visual states via icon color:
  - Gray:   Idle / ready
  - Blue:   Loading models (first activation)
  - Green:  Listening / recording
  - Amber:  Transcribing (processing audio)

Left-click stops recording. Right-click for menu.
Hotkey (Ctrl+Shift+Space) starts/stops dictation.
"""

import logging
import os
import subprocess

from PIL import Image, ImageDraw
import pystray

import autostart
import config
from recorder import FORMATS, RECORDING_DIR

logger = logging.getLogger(__name__)

ICON_SIZE = 64

# ─── State Colors ────────────────────────────────────────────

STATE_IDLE = "idle"
STATE_LOADING = "loading"
STATE_LISTENING = "listening"
STATE_TRANSCRIBING = "transcribing"
STATE_RECORDING = "recording"
STATE_SAVING = "saving"
STATE_LECTURE = "lecture"
STATE_LECTURE_PAUSED = "lecture_paused"
STATE_LECTURE_FINISHING = "lecture_finishing"

STATE_COLORS = {
    STATE_IDLE:          "#5a5a5a",   # Gray — nothing happening
    STATE_LOADING:       "#4a90d9",   # Blue — one-time model init
    STATE_LISTENING:     "#4CAF50",   # Green — mic open, recording
    STATE_TRANSCRIBING:  "#e8963a",   # Amber — processing audio
    STATE_RECORDING:     "#5a5a5a",   # Gray — silent background recording
    STATE_SAVING:        "#5a5a5a",   # Gray — saving file
    STATE_LECTURE:           "#d9443a",   # Red — lecture recording
    STATE_LECTURE_PAUSED:    "#9e9e9e",   # Light gray — lecture paused
    STATE_LECTURE_FINISHING: "#e8963a",   # Amber — finishing lecture transcript
}

STATE_TITLES = {
    STATE_IDLE:          "Whisper Dictation — Ready (Ctrl+Shift+Space)",
    STATE_LOADING:       "Whisper Dictation — Loading model…",
    STATE_LISTENING:     "Whisper Dictation — Listening…",
    STATE_TRANSCRIBING:  "Whisper Dictation — Transcribing…",
    STATE_RECORDING:     "Whisper Dictation — Recording…",
    STATE_SAVING:        "Whisper Dictation — Saving recording…",
    STATE_LECTURE:           "Lecture — recording…",
    STATE_LECTURE_PAUSED:    "Lecture — paused",
    STATE_LECTURE_FINISHING: "Lecture — finishing transcript…",
}


def _create_icon_image(state: str = STATE_IDLE) -> Image.Image:
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    color = STATE_COLORS.get(state, STATE_COLORS[STATE_IDLE])
    cx, cy = ICON_SIZE // 2, ICON_SIZE // 2

    # Microphone body
    mic_w, mic_h = 20, 28
    mic_top = cy - 16
    draw.rounded_rectangle(
        [cx - mic_w // 2, mic_top, cx + mic_w // 2, mic_top + mic_h],
        radius=mic_w // 2,
        fill=color,
    )

    # Arc below mic
    arc_y = mic_top + mic_h - 4
    draw.arc(
        [cx - 16, arc_y - 8, cx + 16, arc_y + 16],
        start=0, end=180,
        fill=color, width=3,
    )

    # Stand
    stand_top = arc_y + 12
    draw.line([cx, stand_top, cx, stand_top + 8], fill=color, width=3)
    draw.line([cx - 8, stand_top + 8, cx + 8, stand_top + 8], fill=color, width=3)

    # Recording indicator: small red dot in top-right corner when listening (dictation only)
    if state in (STATE_LISTENING, STATE_LECTURE):
        dot_r = 6
        draw.ellipse(
            [ICON_SIZE - dot_r * 2 - 2, 2, ICON_SIZE - 2, dot_r * 2 + 2],
            fill="#FF3B30",
        )

    return img


# ─── Available models ────────────────────────────────────────
# Grouped by backend. Labels show in the tray menu.

MODELS = {
    # Whisper (faster-whisper)
    "base.en":          {"label": "Whisper Base",          "size": "~150 MB"},
    "small.en":         {"label": "Whisper Small",         "size": "~500 MB"},
    "medium.en":        {"label": "Whisper Medium",        "size": "~1.5 GB"},
    # Distil-Whisper (faster-whisper, distilled)
    "distil-small.en":  {"label": "Distil-Whisper Small",  "size": "~350 MB"},
    "distil-medium.en": {"label": "Distil-Whisper Medium", "size": "~750 MB"},
    # Moonshine (ONNX, by Useful Sensors)
    "moonshine-tiny":   {"label": "Moonshine Tiny",        "size": "~26 MB"},
    "moonshine-base":   {"label": "Moonshine Base",        "size": "~58 MB"},
    # Parakeet CTC (sherpa-onnx, NVIDIA)
    "parakeet-ctc-110m":  {"label": "Parakeet CTC 110M",   "size": "~420 MB"},
    # SenseVoice (sherpa-onnx, Alibaba)
    "sensevoice-small": {"label": "SenseVoice Small",      "size": "~230 MB"},
}


class TrayIcon:
    """
    System tray icon — the app's only UI.

    Callbacks:
        on_toggle()                — toggle dictation (hotkey)
        on_quit()                  — exit the app
        on_model_change(model_id)  — switch Whisper model
        on_mode_change(mode)       — switch hotkey mode (toggle/hold)
        on_sound_toggle(enabled)   — toggle sound feedback
    """

    def __init__(self):
        self.on_toggle = None
        self.on_quit = None
        self.on_model_change = None
        self.on_mode_change = None
        self.on_sound_toggle = None
        self.on_recording_toggle = None
        self.on_recording_format_change = None
        # Lecture mode
        self.on_lecture_start = None          # () → open start dialog
        self.on_lecture_quick_start = None    # () → start with calendar/last course
        self.on_lecture_pause_toggle = None
        self.on_lecture_bookmark = None
        self.on_lecture_stop = None
        self.on_lecture_model_change = None   # (model_id)
        self.on_lecture_library = None
        self.on_calendar_setup = None
        self.lecture_root = ""
        self.quick_start_label = None         # callable → str | None
        self._lecture_model = "small"
        self._state = STATE_IDLE
        self._current_model = "base.en"
        self._hotkey_mode = "toggle"
        self._sound_enabled = True
        self._recording_format = "mp3"
        self._autostart_enabled = autostart.is_enabled()
        self._icon: pystray.Icon | None = None

    @property
    def state(self) -> str:
        return self._state

    def run(self, setup=None):
        self._icon = self._build_icon()
        self._icon.run(setup=setup)

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def notify(self, title: str, message: str):
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception as e:
                logger.debug(f"Notification failed: {e}")

    def set_state(self, state: str):
        """Update icon color and tooltip to reflect current app state."""
        self._state = state
        try:
            if self._icon is not None:
                self._icon.icon = _create_icon_image(state=state)
                self._icon.title = STATE_TITLES.get(state, STATE_TITLES[STATE_IDLE])
                # On Windows pystray only rebuilds the menu on update_menu(),
                # so items that depend on the state (lecture controls,
                # Start/Stop recording) must be refreshed here.
                self._icon.update_menu()
                logger.debug(f"Tray state: {state}")
        except Exception:
            pass

    def set_title(self, title: str):
        try:
            if self._icon is not None:
                self._icon.title = title
        except Exception:
            pass

    def set_current_model(self, model_id: str):
        self._current_model = model_id

    def set_hotkey_mode(self, mode: str):
        self._hotkey_mode = mode

    def set_sound_enabled(self, enabled: bool):
        self._sound_enabled = enabled

    def set_recording_format(self, fmt: str):
        self._recording_format = fmt

    def set_lecture_model(self, model_id: str):
        self._lecture_model = model_id

    def refresh_menu(self):
        try:
            if self._icon is not None:
                self._icon.update_menu()
        except Exception:
            pass

    @property
    def _lecture_running(self) -> bool:
        return self._state in (STATE_LECTURE, STATE_LECTURE_PAUSED)

    @property
    def _lecture_idle(self) -> bool:
        return self._state not in (STATE_LECTURE, STATE_LECTURE_PAUSED,
                                   STATE_LECTURE_FINISHING, STATE_LISTENING,
                                   STATE_TRANSCRIBING, STATE_RECORDING, STATE_LOADING)

    def _quick_label(self) -> str | None:
        try:
            return self.quick_start_label() if self.quick_start_label else None
        except Exception:
            return None

    def _build_icon(self) -> pystray.Icon:
        menu = pystray.Menu(
            # Hidden default item — LEFT-CLICK stops recording
            pystray.MenuItem(
                "Stop",
                self._on_stop,
                default=True,
                visible=False,
            ),
            # Visible menu items for right-click
            pystray.MenuItem(
                "Stop dictation",
                self._on_stop,
                enabled=lambda _: self._state == STATE_LISTENING,
            ),
            pystray.Menu.SEPARATOR,
            # ── Lecture mode ──
            pystray.MenuItem(
                lambda _: f"● Record now: {self._quick_label()}",
                lambda icon, item: self._call(self.on_lecture_quick_start),
                visible=lambda _: self._lecture_idle and bool(self._quick_label()),
            ),
            pystray.MenuItem(
                "Start lecture…",
                lambda icon, item: self._call(self.on_lecture_start),
                visible=lambda _: not self._lecture_running and self._state != STATE_LECTURE_FINISHING,
                enabled=lambda _: self._lecture_idle,
            ),
            pystray.MenuItem(
                lambda _: "Resume lecture" if self._state == STATE_LECTURE_PAUSED else "Pause lecture",
                lambda icon, item: self._call(self.on_lecture_pause_toggle),
                visible=lambda _: self._lecture_running,
            ),
            pystray.MenuItem(
                "Add bookmark  (Ctrl+Shift+B)",
                lambda icon, item: self._call(self.on_lecture_bookmark),
                visible=lambda _: self._lecture_running,
            ),
            pystray.MenuItem(
                "■ Stop lecture",
                lambda icon, item: self._call(self.on_lecture_stop),
                visible=lambda _: self._lecture_running,
            ),
            pystray.MenuItem(
                "Finishing transcript…",
                lambda icon, item: None,
                enabled=False,
                visible=lambda _: self._state == STATE_LECTURE_FINISHING,
            ),
            pystray.MenuItem(
                "Lecture library",
                lambda icon, item: self._call(self.on_lecture_library),
            ),
            pystray.MenuItem(
                "Lecture settings",
                pystray.Menu(
                    pystray.MenuItem(
                        "Model",
                        pystray.Menu(*self._build_lecture_model_menu()),
                    ),
                    pystray.MenuItem(
                        "Calendar / timetable…",
                        lambda icon, item: self._call(self.on_calendar_setup),
                    ),
                    pystray.MenuItem(
                        "Open lectures folder",
                        self._on_open_lectures,
                    ),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Model",
                pystray.Menu(*self._build_model_menu()),
            ),
            pystray.MenuItem(
                "Hotkey mode",
                pystray.Menu(*self._build_mode_menu()),
            ),
            pystray.MenuItem(
                "Sound feedback",
                self._on_sound_toggle,
                checked=lambda _: self._sound_enabled,
            ),
            pystray.MenuItem(
                "Start with Windows",
                self._on_autostart_toggle,
                checked=lambda _: self._autostart_enabled,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _: "Stop recording" if self._state == STATE_RECORDING else "Start recording",
                self._on_recording_toggle,
            ),
            pystray.MenuItem(
                "Recording format",
                pystray.Menu(*self._build_format_menu()),
            ),
            pystray.MenuItem(
                "Open recordings folder",
                self._on_open_recordings,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Transcription history",
                self._on_history,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

        return pystray.Icon(
            name="whisper-dictation",
            icon=_create_icon_image(state=STATE_IDLE),
            title="Whisper Dictation — Starting…",
            menu=menu,
        )

    def _build_model_menu(self):
        """Build model selection menu items with native checkmarks."""
        items = []
        for mid, info in MODELS.items():
            def _make_label(_, mid=mid, info=info):
                return f"{info['label']}  ({info['size']})"

            def _make_checked(_, mid=mid):
                return self._current_model == mid

            def _make_action(mid=mid):
                def _action(icon, item):
                    self._on_model_select(mid)
                return _action

            items.append(pystray.MenuItem(
                _make_label,
                _make_action(mid),
                checked=_make_checked,
            ))
        return items

    def _build_mode_menu(self):
        """Build hotkey mode selection menu items with native checkmarks."""
        modes = [
            ("toggle", "Toggle (press to start/stop)"),
            ("hold",   "Hold (hold to record, release to stop)"),
        ]
        items = []
        for mode_id, label in modes:
            def _make_checked(_, mode_id=mode_id):
                return self._hotkey_mode == mode_id

            def _make_action(mode_id=mode_id):
                def _action(icon, item):
                    self._on_mode_select(mode_id)
                return _action

            items.append(pystray.MenuItem(
                label,
                _make_action(mode_id),
                checked=_make_checked,
            ))
        return items

    def _build_format_menu(self):
        items = []
        for fmt_id, info in FORMATS.items():
            def _make_checked(_, fmt_id=fmt_id):
                return self._recording_format == fmt_id

            def _make_action(fmt_id=fmt_id):
                def _action(icon, item):
                    self._on_format_select(fmt_id)
                return _action

            items.append(pystray.MenuItem(
                info["label"],
                _make_action(fmt_id),
                checked=_make_checked,
            ))
        return items

    def _build_lecture_model_menu(self):
        items = []
        for mid, info in config.LECTURE_MODELS.items():
            def _make_checked(_, mid=mid):
                return self._lecture_model == mid

            def _make_action(mid=mid):
                def _action(icon, item):
                    if mid != self._lecture_model:
                        self._lecture_model = mid
                        self._call(self.on_lecture_model_change, mid)
                return _action

            items.append(pystray.MenuItem(
                f"{info['label']}  ({info['size']})",
                _make_action(mid),
                checked=_make_checked,
                radio=True,
            ))
        return items

    def _call(self, callback, *args):
        if callback:
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"Menu action failed: {e}")

    def _on_open_lectures(self, icon, item):
        try:
            os.makedirs(self.lecture_root, exist_ok=True)
            os.startfile(self.lecture_root)
        except Exception as e:
            logger.error(f"Could not open lectures folder: {e}")

    def _on_format_select(self, fmt: str):
        if fmt == self._recording_format:
            return
        self._recording_format = fmt
        logger.info(f"Recording format changed to: {fmt}")
        if self.on_recording_format_change:
            self.on_recording_format_change(fmt)

    def _on_open_recordings(self, icon, item):
        try:
            os.makedirs(RECORDING_DIR, exist_ok=True)
            os.startfile(RECORDING_DIR)
        except Exception as e:
            logger.error(f"Could not open recordings folder: {e}")

    def _on_recording_toggle(self, icon, item):
        if self.on_recording_toggle:
            self.on_recording_toggle()

    def _on_stop(self, icon, item):
        """Left-click / menu stop — only stops, never starts."""
        if self._state == STATE_LISTENING and self.on_toggle:
            self.on_toggle()

    def _on_model_select(self, model_id: str):
        if model_id == self._current_model:
            return
        self._current_model = model_id
        logger.info(f"Model selection changed to: {model_id}")
        if self.on_model_change:
            self.on_model_change(model_id)

    def _on_mode_select(self, mode: str):
        if mode == self._hotkey_mode:
            return
        self._hotkey_mode = mode
        logger.info(f"Hotkey mode changed to: {mode}")
        if self.on_mode_change:
            self.on_mode_change(mode)

    def _on_sound_toggle(self, icon, item):
        self._sound_enabled = not self._sound_enabled
        logger.info(f"Sound feedback: {'on' if self._sound_enabled else 'off'}")
        if self.on_sound_toggle:
            self.on_sound_toggle(self._sound_enabled)

    def _on_autostart_toggle(self, icon, item):
        self._autostart_enabled = not self._autostart_enabled
        autostart.set_enabled(self._autostart_enabled)
        logger.info(f"Auto-start: {'on' if self._autostart_enabled else 'off'}")

    def _on_history(self, icon, item):
        """Open the transcription history as a styled HTML page."""
        try:
            from history import open_history
            open_history()
        except Exception as e:
            logger.error(f"Could not open history: {e}")

    def _on_quit(self, icon, item):
        if self.on_quit:
            self.on_quit()
