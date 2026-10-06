"""
Lecture Session — record a whole lecture and transcribe it live in chunks.

  Mic (16 kHz) → audio.wav on disk (crash-safe, header kept valid)
               → pending buffer → cut at a pause every ~60 s → work queue
  Worker       → transcribe_segments(chunk) → transcript.md / .srt (appended)

  Stop:  flush the last chunk → drain the queue ("Finishing transcript…")
         → audio.wav → audio.mp3 → status "complete"

Timestamps refer to the saved audio (pauses are not recorded), so they line
up when you play audio.mp3 next to the transcript.

CLI (crash recovery / re-run):
  python lecture.py --transcribe "<session folder>"
"""

import logging
import os
import queue
import sys
import threading
import time

import numpy as np

import config
import lecture_store as store

logger = logging.getLogger(__name__)

STATE_LECTURE = "lecture"
STATE_LECTURE_PAUSED = "lecture_paused"
STATE_LECTURE_FINISHING = "lecture_finishing"


def context_prompt(meta: dict) -> str | None:
    """'Seminar: M13 Multivariate Verfahren …' — primes Whisper with the subject's vocabulary."""
    course = (meta.get("course") or "").strip()
    if not course:
        return None
    kind = (meta.get("kind") or "").strip()
    return f"{kind}: {course}." if kind else f"{course}."


class LanguageLock:
    """
    “Auto-detect” settles on the first language heard clearly.

    Detecting per chunk let quiet or noisy minutes come out as Russian or
    Polish gibberish; once a chunk has real speech, keep its language.
    """

    MIN_SEGMENTS = 3

    def __init__(self, language: str | None):
        self.chosen = None if language in (None, "", "auto") else language

    @property
    def current(self) -> str:
        return self.chosen or "auto"

    def observe(self, detected: str | None, n_segments: int) -> bool:
        """Returns True when this chunk settled the language."""
        if self.chosen is None and detected and n_segments >= self.MIN_SEGMENTS:
            self.chosen = detected
            return True
        return False


class LectureSession:
    """
    One recorded lecture. Create, start(), optionally pause()/bookmark(),
    then stop(). stop() returns immediately; transcription finishes in the
    background and on_finished(session_dir) fires at the end.
    """

    def __init__(self, transcriber, root: str):
        self.transcriber = transcriber
        self.root = root
        self.sample_rate = config.SAMPLE_RATE

        self.on_state_change = None    # callback(state)
        self.on_title_update = None    # callback(title)
        self.on_finished = None        # callback(session_dir, meta)

        self.session_dir: str | None = None
        self.meta: dict = {}
        self.writer: store.TranscriptWriter | None = None
        self.language = "auto"
        self._lang = LanguageLock("auto")
        self._prompt: str | None = None

        self._audio_queue: queue.Queue = queue.Queue(maxsize=2000)
        self._work_queue: queue.Queue = queue.Queue()
        self._active = threading.Event()
        self._paused = threading.Event()
        self._finishing = threading.Event()
        self._stream = None
        self._wav: store.WavAppender | None = None
        self._pipeline_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._meta_lock = threading.Lock()

        self._pending: list[np.ndarray] = []
        self._pending_samples = 0
        self._pending_offset_s = 0.0

    # ─── State ──────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._active.is_set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    @property
    def is_finishing(self) -> bool:
        return self._finishing.is_set()

    @property
    def recorded_s(self) -> float:
        return self._wav.duration_s if self._wav else 0.0

    @property
    def backlog_chunks(self) -> int:
        return self._work_queue.qsize()

    def _emit_state(self, state: str):
        if self.on_state_change:
            try:
                self.on_state_change(state)
            except Exception as e:
                logger.debug(f"state callback failed: {e}")

    # ─── Lifecycle ──────────────────────────────────────────────

    def start(self, course: str, title: str, language: str = "auto",
              calendar: dict | None = None, input_stream_factory=None):
        """
        Open the mic and begin recording. input_stream_factory(callback) can
        replace sounddevice for tests; it must return an object with
        start()/stop()/close().
        """
        if self._active.is_set():
            return
        self.language = language or "auto"
        self.session_dir = store.create_session(
            self.root, course, title, self.language,
            model=getattr(self.transcriber, "model_id", ""),
            calendar=calendar,
        )
        self.meta = store.load_meta(self.session_dir)
        self._lang = LanguageLock(self.language)
        self._prompt = context_prompt(self.meta)
        self.writer = store.TranscriptWriter(self.session_dir, config.LECTURE_PARAGRAPH_S)
        self.writer.write_header(self.meta)
        self._wav = store.WavAppender(
            os.path.join(self.session_dir, store.AUDIO_WAV), self.sample_rate
        )
        self._pending.clear()
        self._pending_samples = 0
        self._pending_offset_s = 0.0

        self._active.set()
        self._paused.clear()

        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="lecture-transcribe", daemon=True
        )
        self._worker_thread.start()
        self._pipeline_thread = threading.Thread(
            target=self._pipeline_loop, name="lecture-pipeline", daemon=True
        )
        self._pipeline_thread.start()

        try:
            if input_stream_factory is not None:
                self._stream = input_stream_factory(self._audio_callback)
            else:
                import sounddevice as sd
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    blocksize=config.BLOCK_SIZE * 4,
                    callback=self._audio_callback,
                )
            self._stream.start()
        except Exception:
            # No mic / device busy: undo everything, leave no empty session behind
            self._active.clear()
            self._work_queue.put(None)
            self._pipeline_thread.join(timeout=2)
            self._wav.close()
            self._stream = None
            import shutil
            shutil.rmtree(self.session_dir, ignore_errors=True)
            raise

        logger.info(f"Lecture STARTED: {self.session_dir}")
        self._emit_state(STATE_LECTURE)
        threading.Thread(target=self._timer_loop, daemon=True).start()

    def pause(self):
        if self._active.is_set() and not self._paused.is_set():
            self._paused.set()
            logger.info("Lecture paused")
            self._emit_state(STATE_LECTURE_PAUSED)

    def resume(self):
        if self._active.is_set() and self._paused.is_set():
            self._paused.clear()
            logger.info("Lecture resumed")
            self._emit_state(STATE_LECTURE)

    def bookmark(self, label: str = "Bookmark") -> float | None:
        """Mark the current moment in the transcript."""
        if not self._active.is_set() or self.writer is None:
            return None
        t = self.recorded_s
        self.writer.add_bookmark(t, label)
        with self._meta_lock:
            self.meta.setdefault("bookmarks", []).append({"t": round(t, 1), "label": label})
            self._save_meta()
        logger.info(f"Bookmark at {store.format_ts(t)}")
        return t

    def stop(self):
        """Stop recording; transcription of the rest continues in background."""
        if not self._active.is_set():
            return
        self._active.clear()
        self._paused.clear()
        self._finishing.set()

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.warning(f"Closing mic stream failed: {e}")
            self._stream = None

        self._emit_state(STATE_LECTURE_FINISHING)

        with self._meta_lock:
            self.meta["status"] = store.STATUS_FINISHING
            self._save_meta()

        threading.Thread(target=self._finish, name="lecture-finish", daemon=True).start()

    def wait_finished(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.time() + timeout
        while self._finishing.is_set():
            if deadline is not None and time.time() > deadline:
                return False
            time.sleep(0.1)
        return True

    # ─── Audio path ─────────────────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            logger.warning(f"Lecture audio status: {status}")
        try:
            self._audio_queue.put_nowait(indata.copy())
        except queue.Full:
            logger.warning("Lecture audio queue full — dropping a block")

    def _pipeline_loop(self):
        chunk_samples = int(config.LECTURE_CHUNK_S * self.sample_rate)
        search_samples = int(config.LECTURE_CUT_SEARCH_S * self.sample_rate)

        while self._active.is_set() or not self._audio_queue.empty():
            try:
                block = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._paused.is_set():
                continue
            block = block.reshape(-1).astype(np.float32)
            self._wav.write(block)
            self._pending.append(block)
            self._pending_samples += len(block)

            if self._pending_samples >= chunk_samples + search_samples:
                self._cut_chunk(chunk_samples)

        # Recording stopped: everything left is the final chunk
        if self._pending_samples:
            audio = np.concatenate(self._pending)
            self._work_queue.put((self._pending_offset_s, audio))
            self._pending.clear()
            self._pending_samples = 0

    def _cut_chunk(self, chunk_samples: int):
        audio = np.concatenate(self._pending)
        cut = store.find_cut_point(
            audio[:chunk_samples + int(config.LECTURE_CUT_SEARCH_S * self.sample_rate)],
            self.sample_rate,
            search_s=config.LECTURE_CUT_SEARCH_S * 2,
        )
        cut = max(cut, int(self.sample_rate))  # never emit tiny chunks
        self._work_queue.put((self._pending_offset_s, audio[:cut]))
        rest = audio[cut:]
        self._pending = [rest] if len(rest) else []
        self._pending_samples = len(rest)
        self._pending_offset_s += cut / self.sample_rate

    # ─── Transcription worker ───────────────────────────────────

    def _worker_loop(self):
        while True:
            item = self._work_queue.get()
            if item is None:
                return
            offset_s, audio = item
            self._transcribe_chunk(offset_s, audio)

    def _transcribe_chunk(self, offset_s: float, audio: np.ndarray):
        duration = len(audio) / self.sample_rate
        if duration < config.MIN_SPEECH_DURATION_S:
            return
        try:
            segments, detected = self.transcriber.transcribe_segments(
                audio, self._lang.current, prompt=self._prompt)
            self.writer.append_segments(segments, offset_s)
            if self._lang.observe(detected, len(segments)):
                logger.info(f"Lecture language settled on '{detected}'")
                # Remember it for the course, so next time it starts out right
                course = self.meta.get("course", "")
                if course and store.load_course(store.course_dir(self.root, course))["language"] == "auto":
                    store.ensure_course(self.root, course, detected)
            with self._meta_lock:
                if detected and detected not in self.meta.setdefault("detected_languages", []):
                    self.meta["detected_languages"].append(detected)
                self.meta["transcribed_until_s"] = round(offset_s + duration, 1)
                self._save_meta()
        except Exception as e:
            logger.error(f"Lecture chunk at {store.format_ts(offset_s)} failed: {e}")
            self.writer.append_note(offset_s, f"Transcription failed for this part ({e}). "
                                              "Use “Transcribe again” in the library.")
            with self._meta_lock:
                self.meta["failed_chunks"] = self.meta.get("failed_chunks", 0) + 1
                self._save_meta()

    def _finish(self):
        try:
            if self._pipeline_thread is not None:
                self._pipeline_thread.join()
            self._work_queue.put(None)
            if self._worker_thread is not None:
                self._worker_thread.join()

            self.writer.finish()
            wav_path = self._wav.path
            self._wav.close()

            with self._meta_lock:
                self.meta["duration_s"] = round(self.recorded_s, 1)
                self.meta["transcribed_until_s"] = self.meta["duration_s"]
                self.meta["status"] = (store.STATUS_INCOMPLETE if self.meta.get("failed_chunks")
                                       else store.STATUS_COMPLETE)
                self._save_meta()
            store.rewrite_front_matter(self.session_dir, self.meta)

            _convert_audio(self.session_dir, wav_path)
            logger.info(f"Lecture finished: {self.session_dir} "
                        f"({store.format_duration(self.meta['duration_s'])})")
        except Exception as e:
            logger.error(f"Finishing lecture failed: {e}")
        finally:
            self._finishing.clear()
            self._emit_state("idle")
            if self.on_finished:
                try:
                    self.on_finished(self.session_dir, dict(self.meta))
                except Exception as e:
                    logger.debug(f"on_finished failed: {e}")

    def _save_meta(self):
        try:
            store.save_meta(self.session_dir, self.meta)
        except Exception as e:
            logger.warning(f"Saving lecture meta failed: {e}")

    # ─── Tooltip ────────────────────────────────────────────────

    def _timer_loop(self):
        course = self.meta.get("course", "")
        short = course.split(" ", 1)[0] if course[:1] == "M" and course[1:2].isdigit() else course[:24]
        while self._active.is_set():
            rec = store.format_duration(self.recorded_s)
            done = store.format_duration(self.meta.get("transcribed_until_s", 0))
            prefix = "Lecture paused" if self._paused.is_set() else "Lecture"
            if self.on_title_update:
                self.on_title_update(f"{prefix} — {short} — {rec} (text to {done})")
            time.sleep(1.0)


def _convert_audio(session_dir: str, wav_path: str):
    """WAV → MP3, delete WAV unless the user wants to keep it."""
    try:
        import settings
        keep_wav = settings.get("lecture_keep_wav", False)
    except Exception:
        keep_wav = False
    mp3_path = os.path.join(session_dir, store.AUDIO_MP3)
    try:
        store.encode_mp3(wav_path, mp3_path, config.LECTURE_MP3_BITRATE)
        if not keep_wav:
            os.remove(wav_path)
    except Exception as e:
        logger.warning(f"MP3 conversion failed, keeping WAV: {e}")


# ─── Offline re-transcription (CLI) ──────────────────────────

def load_audio_16k(path: str) -> np.ndarray:
    """Load any audio file as 16 kHz mono float32."""
    if path.lower().endswith(".wav"):
        store.repair_wav(path)
        audio, sr = store.read_wav(path)
        if sr == config.SAMPLE_RATE:
            return audio
    from faster_whisper import decode_audio
    return decode_audio(path, sampling_rate=config.SAMPLE_RATE)


def retranscribe(session_dir: str, model_id: str | None = None, on_progress=None) -> dict:
    """Transcribe a session's saved audio from scratch (overwrites transcript)."""
    from transcriber import Transcriber
    import settings

    meta = store.load_meta(session_dir)
    audio_path = store.find_audio(session_dir)
    if not audio_path:
        raise FileNotFoundError(f"No audio in {session_dir}")

    meta["status"] = store.STATUS_FINISHING
    store.save_meta(session_dir, meta)

    audio = load_audio_16k(audio_path)
    sr = config.SAMPLE_RATE
    model_id = model_id or settings.get("lecture_model", "small")
    transcriber = Transcriber(model_id)
    transcriber.load(on_progress=on_progress)

    meta.update({
        "model": model_id,
        "duration_s": round(len(audio) / sr, 1),
        "detected_languages": [],
        "failed_chunks": 0,
        "transcribed_until_s": 0.0,
    })
    writer = store.TranscriptWriter(session_dir, config.LECTURE_PARAGRAPH_S)
    writer.write_header(meta)
    for b in meta.get("bookmarks", []):
        writer.add_bookmark(b["t"], b.get("label", "Bookmark"))

    chunks = store.split_chunks(audio, sr, config.LECTURE_CHUNK_S, config.LECTURE_CUT_SEARCH_S)
    lang = LanguageLock(meta.get("language", "auto"))
    prompt = context_prompt(meta)
    for i, (offset_s, chunk) in enumerate(chunks):
        try:
            segments, detected = transcriber.transcribe_segments(chunk, lang.current, prompt=prompt)
            lang.observe(detected, len(segments))
            writer.append_segments(segments, offset_s)
            if detected and detected not in meta["detected_languages"]:
                meta["detected_languages"].append(detected)
        except Exception as e:
            logger.error(f"Chunk {i} failed: {e}")
            writer.append_note(offset_s, f"Transcription failed for this part ({e}).")
            meta["failed_chunks"] += 1
        meta["transcribed_until_s"] = round(offset_s + len(chunk) / sr, 1)
        store.save_meta(session_dir, meta)
        if on_progress:
            on_progress(f"{(i + 1) / len(chunks):.0%}")

    writer.finish()
    meta["status"] = store.STATUS_INCOMPLETE if meta["failed_chunks"] else store.STATUS_COMPLETE
    store.save_meta(session_dir, meta)
    store.rewrite_front_matter(session_dir, meta)
    if audio_path.lower().endswith(".wav"):
        _convert_audio(session_dir, audio_path)
    return meta


def _limit_cpu():
    """Same limits as the tray app, so a background re-transcription doesn't slow the laptop."""
    try:
        import psutil
        p = psutil.Process()
        if config.LOW_PRIORITY:
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        n = config.MAX_INFERENCE_THREADS
        count = psutil.cpu_count(logical=True)
        if count and count > n:
            p.cpu_affinity(list(range(count - n, count)))
    except Exception as e:
        logger.warning(f"Could not set CPU limits: {e}")


def main(argv: list[str]):
    import argparse
    parser = argparse.ArgumentParser(description="Lecture transcription tools")
    parser.add_argument("--transcribe", metavar="SESSION_DIR",
                        help="(Re-)transcribe a lecture session's audio")
    parser.add_argument("--model", help="Model id (default: lecture_model setting)")
    args = parser.parse_args(argv)

    if args.transcribe:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        _limit_cpu()
        def say(msg):
            if sys.stdout is not None:   # None under pythonw.exe
                print(msg, flush=True)

        meta = retranscribe(args.transcribe, args.model,
                            on_progress=lambda m: say(f"PROGRESS {m}"))
        say(f"DONE {meta['status']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main(sys.argv[1:])
