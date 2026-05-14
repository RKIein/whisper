"""
Whisper Dictation App — tray-only Windows dictation tool.

CPU-optimized: thread pinning + low priority so dictation
never hogs the system. Lazy-loaded model with hot-swapping.

  Ctrl+Shift+Space  →  start/stop dictation (toggle or hold mode)
  Escape            →  cancel recording (discard)
  Left-click tray   →  stop recording
  Right-click tray  →  menu (model, mode, sound, quit)

Icon states:
  Gray   → idle
  Blue   → loading / switching model
  Green  → listening (recording)
  Amber  → transcribing
"""

import io
import logging
import logging.handlers
import os
import sys
import threading

# ─── CPU Optimization (must be set before any model imports) ──

os.environ["OMP_NUM_THREADS"] = "4"
os.environ["MKL_NUM_THREADS"] = "4"
os.environ["CT2_INTRA_THREADS"] = "4"
os.environ["ONNXRUNTIME_THREAD_COUNT"] = "4"

# pythonw.exe sets stdout/stderr to None
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

import config
import settings
import sounds
from transcriber import Transcriber
from audio_engine import AudioEngine
from text_injector import TextInjector
from hotkeys import HotkeyManager
from tray import TrayIcon, STATE_IDLE, STATE_LOADING
from recorder import VoiceRecorder

# ─── Logging ─────────────────────────────────────────────────

log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whisper.log")
log_handlers = [
    logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=1, encoding="utf-8"
    )
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=log_handlers,
)
logger = logging.getLogger("whisper-app")


def _apply_cpu_limits():
    """Set low process priority and optionally pin to specific cores."""
    try:
        import psutil
        p = psutil.Process(os.getpid())

        if config.LOW_PRIORITY:
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            logger.info("Process priority set to BELOW_NORMAL")

        cpu_count = psutil.cpu_count(logical=True)
        n_threads = config.MAX_INFERENCE_THREADS
        if cpu_count and cpu_count > n_threads:
            cores = list(range(cpu_count - n_threads, cpu_count))
            p.cpu_affinity(cores)
            logger.info(f"CPU affinity set to cores {cores}")

    except Exception as e:
        logger.warning(f"Could not set CPU limits: {e}")


class WhisperDictationApp:

    def __init__(self):
        # Load saved settings
        self._settings = settings.load()
        config.WHISPER_MODEL_FINAL = self._settings.get("model", "base.en")

        self.transcriber = Transcriber()
        self.injector = TextInjector()
        self.engine = AudioEngine(self.transcriber, self.injector)
        self.recorder = VoiceRecorder()
        self.hotkeys = HotkeyManager()
        self.tray = TrayIcon()
        self._loading = False

        # Apply saved preferences
        sounds.set_enabled(self._settings.get("sound_feedback", True))
        self.hotkeys.mode = self._settings.get("hotkey_mode", "toggle")
        self.recorder.format = self._settings.get("recording_format", "mp3")

    def run(self):
        _apply_cpu_limits()

        # Wire hotkeys
        self.hotkeys.on_toggle_dictation = self._toggle_dictation
        self.hotkeys.on_cancel_dictation = self._cancel_dictation
        self.hotkeys.on_stop_dictation = self._stop_dictation

        # Wire engine
        self.engine.on_state_change = self._on_state_change
        self.engine.on_title_update = lambda t: self.tray.set_title(t)

        # Wire recorder
        self.recorder.on_state_change = self._on_recorder_state_change
        self.recorder.on_title_update = lambda t: self.tray.set_title(t)
        self.recorder.on_recording_saved = self._on_recording_saved

        # Wire tray
        self.tray.on_toggle = self._toggle_dictation
        self.tray.on_quit = self._quit
        self.tray.on_model_change = self._on_model_change
        self.tray.on_mode_change = self._on_mode_change
        self.tray.on_sound_toggle = self._on_sound_toggle
        self.tray.on_recording_toggle = self._toggle_recording
        self.tray.on_recording_format_change = self._on_recording_format_change
        self.tray.set_current_model(config.WHISPER_MODEL_FINAL)
        self.tray.set_hotkey_mode(self.hotkeys.mode)
        self.tray.set_sound_enabled(self._settings.get("sound_feedback", True))
        self.tray.set_recording_format(self.recorder.format)

        self.hotkeys.start()
        self.tray.run(setup=self._on_tray_ready)

    def _on_tray_ready(self, icon):
        icon.visible = True
        self.tray.set_state(STATE_LOADING)
        logger.info(f"Startup. Model: {config.WHISPER_MODEL_FINAL}, "
                     f"Mode: {self.hotkeys.mode}, "
                     f"Sound: {self._settings.get('sound_feedback', True)}")
        # Preload models in background so first hotkey press is instant
        self._preload()

    # ─── Model Loading ──────────────────────────────────────────

    def _preload(self):
        """Preload model at startup so first activation is instant."""
        self._loading = True

        def _do_preload():
            try:
                def _progress(msg):
                    self.tray.set_title(f"Whisper Dictation — {msg}")

                self.transcriber.load(on_progress=_progress)
                self.tray.set_state(STATE_IDLE)
                logger.info(f"Preload complete: {self.transcriber.model_id}")
            except Exception as e:
                logger.error(f"Preload failed: {e}")
                self.tray.set_state(STATE_IDLE)
                self.tray.set_title("Whisper Dictation — Error")
            finally:
                self._loading = False

        threading.Thread(target=_do_preload, daemon=True).start()

    def _ensure_loaded(self, then_activate=False):
        if self.transcriber.is_loaded:
            if then_activate:
                self.engine.activate()
            return

        if self._loading:
            return

        self._loading = True
        self.tray.set_state(STATE_LOADING)

        def _load():
            try:
                def _progress(msg):
                    self.tray.set_title(f"Whisper Dictation — {msg}")

                self.transcriber.load(on_progress=_progress)
                self.tray.set_state(STATE_IDLE)
                logger.info(f"Model loaded: {self.transcriber.model_id}")
                self._loading = False

                if then_activate:
                    self.engine.activate()

            except Exception as e:
                logger.error(f"Failed to load: {e}")
                self.tray.set_state(STATE_IDLE)
                self.tray.set_title("Whisper Dictation — Error")
                self._loading = False

        threading.Thread(target=_load, daemon=True).start()

    # ─── Dictation Control ──────────────────────────────────────

    def _toggle_dictation(self):
        if self._loading:
            return
        if self.recorder.is_active:
            return
        if self.engine._finalizing.is_set():
            logger.debug("Ignoring toggle — still finalizing")
            return
        if self.engine.is_active:
            self.engine.deactivate()
        else:
            self._ensure_loaded(then_activate=True)

    def _stop_dictation(self):
        """Stop only (for hold mode release)."""
        if self.engine.is_active:
            self.engine.deactivate()

    def _cancel_dictation(self):
        """Cancel recording — discard without transcribing."""
        if self.engine.is_active:
            self.engine.cancel()

    # ─── Recording Control ──────────────────────────────────────

    def _toggle_recording(self):
        if self.engine.is_active or self._loading or self.engine._finalizing.is_set():
            return
        if self.recorder.is_active:
            self.recorder.stop()
        else:
            self.recorder.start()

    def _on_recorder_state_change(self, state: str):
        self.tray.set_state(state)

    def _on_recording_saved(self, filepath, duration):
        logger.info(f"Recording saved: {filepath} ({duration:.1f}s)")

    def _on_recording_format_change(self, fmt: str):
        self.recorder.format = fmt
        settings.put("recording_format", fmt)
        logger.info(f"Recording format: {fmt}")

    # ─── Settings Callbacks ─────────────────────────────────────

    def _on_model_change(self, model_id: str):
        """Switch model — only allowed when idle."""
        if self.engine.is_active or self._loading or self.engine._finalizing.is_set():
            logger.warning("Cannot switch model while active/loading/finalizing")
            return

        settings.put("model", model_id)

        if not self.transcriber.is_loaded:
            config.WHISPER_MODEL_FINAL = model_id
            self.tray.set_current_model(model_id)
            logger.info(f"Model pre-selected: {model_id}")
            return

        self._loading = True
        self.tray.set_state(STATE_LOADING)

        def _switch():
            try:
                from tray import MODELS
                size = MODELS.get(model_id, {}).get("size", "")
                size_note = f" ({size})" if size else ""

                def _progress(msg):
                    self.tray.set_title(f"Whisper Dictation — {msg}{size_note}")

                self.transcriber.switch_model(model_id, on_progress=_progress)
                config.WHISPER_MODEL_FINAL = model_id
                self.tray.set_current_model(model_id)
                self.tray.set_state(STATE_IDLE)
                logger.info(f"Model switched to: {model_id}")
            except Exception as e:
                logger.error(f"Failed to switch model: {e}")
                self.tray.set_state(STATE_IDLE)
            finally:
                self._loading = False

        threading.Thread(target=_switch, daemon=True).start()

    def _on_mode_change(self, mode: str):
        self.hotkeys.mode = mode
        self.tray.set_hotkey_mode(mode)
        settings.put("hotkey_mode", mode)
        logger.info(f"Hotkey mode switched to: {mode}")

    def _on_sound_toggle(self, enabled: bool):
        sounds.set_enabled(enabled)
        self.tray.set_sound_enabled(enabled)
        settings.put("sound_feedback", enabled)
        logger.info(f"Sound feedback: {'on' if enabled else 'off'}")

    # ─── Lifecycle ──────────────────────────────────────────────

    def _quit(self):
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True
        logger.info("Shutting down...")
        self.hotkeys.stop()
        if self.recorder.is_active:
            self.recorder.stop()
        if self.engine.is_active:
            self.engine.deactivate()
        # Wait briefly for finalization, then quit regardless
        import time
        deadline = time.time() + 10
        while self.engine._finalizing.is_set() and time.time() < deadline:
            time.sleep(0.1)
        self.tray.stop()

    def _on_state_change(self, state: str):
        self.tray.set_state(state)


if __name__ == "__main__":
    app = WhisperDictationApp()
    app.run()
