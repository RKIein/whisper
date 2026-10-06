"""
Whisper Dictation App — tray-only Windows dictation tool.

CPU-optimized: thread pinning + low priority so dictation
never hogs the system. Lazy-loaded model with hot-swapping.

  Ctrl+Shift+Space  →  start/stop dictation (toggle or hold mode)
  Escape            →  cancel recording (discard)
  Ctrl+Shift+B      →  bookmark (while recording a lecture)
  Left-click tray   →  stop recording
  Right-click tray  →  menu (model, mode, sound, lecture mode, quit)

Lecture mode (tray → Start lecture…): records a whole lecture, transcribes
it live in chunks and files it under Documents/Lectures/<Course>/. With a
timetable (.ics) set up, course/type/room/lecturer come from the calendar.

Icon states:
  Gray   → idle
  Blue   → loading / switching model
  Green  → listening (recording)
  Amber  → transcribing (or finishing a lecture transcript)
  Red    → recording a lecture
"""

import io
import json
import logging
import logging.handlers
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

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
import course_calendar
import launcher
import lecture_store
from lecture import LectureSession

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

        # Lecture mode
        self.lecture: LectureSession | None = None
        self.lecture_transcriber: Transcriber | None = None
        self._lecture_starting = False
        self._lecture_reminded = False
        self.calendar = course_calendar.CourseCalendar(url=self._settings.get("calendar_url", ""))
        self._current_event: course_calendar.CalendarEvent | None = None

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
        self.hotkeys.on_bookmark = self._lecture_bookmark

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

        # Wire lecture mode
        self.tray.on_lecture_start = self._lecture_dialog
        self.tray.on_lecture_quick_start = self._lecture_quick_start
        self.tray.on_lecture_pause_toggle = self._lecture_pause_toggle
        self.tray.on_lecture_bookmark = self._lecture_bookmark
        self.tray.on_lecture_stop = self._lecture_stop
        self.tray.on_lecture_model_change = self._on_lecture_model_change
        self.tray.on_lecture_library = lambda: launcher.spawn("library")
        self.tray.on_calendar_setup = lambda: launcher.spawn("calendar_setup")
        self.tray.lecture_root = self._lecture_root
        self.tray.quick_start_label = self._quick_start_label
        self.tray.set_lecture_model(self._settings.get("lecture_model", "small"))

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
        threading.Thread(target=self._calendar_loop, daemon=True).start()
        self._report_unfinished_lectures()

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
        if self._loading or self._lecture_blocking:
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
        if self._lecture_blocking:
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
        lecture = self.lecture
        if lecture is not None:
            if lecture.is_active:
                lecture.stop()
            # Give the transcript a minute to finish; anything left over can be
            # finished later via Lecture library → Transcribe again.
            lecture.wait_finished(timeout=60)
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

    # ─── Lecture Mode ───────────────────────────────────────────

    @property
    def _lecture_root(self) -> str:
        return settings.get("lecture_root")

    @property
    def _lecture_blocking(self) -> bool:
        """A lecture is starting, recording or still finishing."""
        return self._lecture_starting or (self.lecture is not None and (
            self.lecture.is_active or self.lecture.is_finishing))

    def _lecture_can_start(self) -> bool:
        return not (self._lecture_blocking or self._loading or self.engine.is_active
                    or self.engine._finalizing.is_set() or self.recorder.is_active)

    def _quick_start_label(self) -> str | None:
        ev = self._current_event
        if ev is not None:
            return f"{ev.short_label} ({ev.start:%H:%M})"
        return None

    def _lecture_busy_notice(self):
        self.tray.notify("Lecture not started",
                         "Wait until loading, dictation or the voice recording has finished.")

    def _lecture_dialog(self):
        if not self._lecture_can_start():
            self._lecture_busy_notice()
            return

        def _run():
            fd, out = tempfile.mkstemp(suffix=".json", prefix="lecture-")
            os.close(fd)
            try:
                if launcher.run("lecture_dialog", "--out", out) != 0:
                    return
                with open(out, "r", encoding="utf-8") as f:
                    choice = json.load(f)
                self._begin_lecture(**choice)
            except Exception as e:
                logger.error(f"Lecture dialog failed: {e}")
            finally:
                try:
                    os.remove(out)
                except OSError:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def _lecture_quick_start(self):
        """One click: start recording the calendar event that's on right now."""
        ev = self._current_event
        if ev is None:
            self._lecture_dialog()
            return
        cdir = lecture_store.course_dir(self._lecture_root, ev.course)
        language = lecture_store.load_course(cdir)["language"] if os.path.isdir(cdir) else "auto"
        self._begin_lecture(
            course=ev.course,
            title=lecture_store.next_title(cdir, language, ev.kind),
            language=language,
            calendar=ev.to_meta(),
        )

    def _begin_lecture(self, course: str, title: str, language: str = "auto",
                       calendar: dict | None = None):
        if not self._lecture_can_start():
            logger.warning("Cannot start lecture now (busy)")
            self._lecture_busy_notice()
            return
        self._lecture_starting = True
        self._lecture_reminded = False
        self.tray.set_state(STATE_LOADING)

        def _start():
            try:
                model_id = settings.get("lecture_model", "small")
                if self.lecture_transcriber is None or self.lecture_transcriber.model_id != model_id:
                    self.lecture_transcriber = Transcriber(model_id)
                self.lecture_transcriber.load(
                    on_progress=lambda m: self.tray.set_title(f"Lecture — {m}")
                )
                lecture_store.ensure_course(self._lecture_root, course, language)

                session = LectureSession(self.lecture_transcriber, self._lecture_root)
                session.on_state_change = self._on_lecture_state
                session.on_title_update = lambda t: self.tray.set_title(t)
                session.on_finished = self._on_lecture_finished
                self.lecture = session
                session.start(course, title, language, calendar=calendar)
                settings.put("last_course", course)
                logger.info(f"Lecture: {course} / {title} ({language})")
            except Exception as e:
                logger.error(f"Could not start lecture: {e}")
                self.lecture = None
                self.tray.set_state(STATE_IDLE)
                self.tray.notify("Lecture not started", str(e)[:200])
            finally:
                self._lecture_starting = False
                self.tray.refresh_menu()

        threading.Thread(target=_start, daemon=True).start()

    def _on_lecture_state(self, state: str):
        self.tray.set_state(STATE_IDLE if state == "idle" else state)
        self.tray.refresh_menu()

    def _on_lecture_finished(self, session_dir: str, meta: dict):
        duration = lecture_store.format_duration(meta.get("duration_s", 0))
        note = "" if meta.get("status") == lecture_store.STATUS_COMPLETE else " (with gaps)"
        self.tray.notify("Lecture saved",
                         f"{meta.get('title', '')} — {meta.get('course', '')} · {duration}{note}")
        # Free the lecture model's memory until the next lecture
        self.lecture = None
        self.lecture_transcriber = None
        self.tray.refresh_menu()

    def _lecture_pause_toggle(self):
        if self.lecture is None or not self.lecture.is_active:
            return
        if self.lecture.is_paused:
            self.lecture.resume()
        else:
            self.lecture.pause()

    def _lecture_bookmark(self):
        if self.lecture is None or not self.lecture.is_active:
            return
        t = self.lecture.bookmark()
        if t is not None:
            self.tray.notify("Bookmark", f"Saved at {lecture_store.format_ts(t)}")

    def _lecture_stop(self):
        if self.lecture is not None and self.lecture.is_active:
            self.lecture.stop()

    def _on_lecture_model_change(self, model_id: str):
        settings.put("lecture_model", model_id)
        self.tray.set_lecture_model(model_id)
        if not self._lecture_blocking:
            self.lecture_transcriber = None   # load the new one at next lecture
        logger.info(f"Lecture model: {model_id}")

    def _calendar_loop(self):
        """Keep 'what's on now' fresh for the tray menu; remind on overrun."""
        while not getattr(self, "_shutting_down", False):
            try:
                self.calendar.url = settings.get("calendar_url", "")
                self._current_event = self.calendar.current() if self.calendar.available else None
                self._check_overrun()
                self.tray.refresh_menu()
            except Exception as e:
                logger.debug(f"Calendar loop: {e}")
            time.sleep(60)

    def _check_overrun(self):
        lec = self.lecture
        if lec is None or not lec.is_active or self._lecture_reminded:
            return
        end = (lec.meta.get("calendar") or {}).get("end")
        if not end:
            return
        limit = datetime.fromisoformat(end) + timedelta(minutes=config.LECTURE_OVERRUN_REMINDER_MIN)
        if datetime.now() > limit:
            self._lecture_reminded = True
            self.tray.notify("Still recording",
                             f"“{lec.meta.get('title', '')}” was scheduled to end at {end[11:16]}.")

    def _report_unfinished_lectures(self):
        try:
            unfinished = [p for p in lecture_store.find_incomplete(self._lecture_root)
                          if not lecture_store.is_session_busy(p)]
        except Exception:
            return
        for path in unfinished:
            meta = lecture_store.load_meta(path)
            meta["status"] = lecture_store.STATUS_INCOMPLETE
            wav = os.path.join(path, lecture_store.AUDIO_WAV)
            if os.path.exists(wav):
                lecture_store.repair_wav(wav)
                meta["duration_s"] = round((os.path.getsize(wav) - 44) / 2 / config.SAMPLE_RATE, 1)
            lecture_store.save_meta(path, meta)
            lecture_store.rewrite_front_matter(path, meta)
        if unfinished:
            n = len(unfinished)
            self.tray.notify(
                "Unfinished lecture" + ("s" if n > 1 else ""),
                f"{n} lecture(s) were interrupted. The audio is saved — open "
                "Lecture library and use “Transcribe again” to finish the transcript.",
            )


if __name__ == "__main__":
    # Helper windows/tools re-launch the (frozen) exe with --<tool>; see launcher.py
    if len(sys.argv) > 1 and sys.argv[1].startswith("--") and sys.argv[1][2:] in launcher.TOOLS:
        import importlib
        tool = importlib.import_module(sys.argv[1][2:])
        sys.exit(tool.main(sys.argv[2:]))

    app = WhisperDictationApp()
    app.run()
