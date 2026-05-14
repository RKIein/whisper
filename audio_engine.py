"""
Audio Engine — record-then-transcribe dictation pipeline.

Architecture:
  While recording (green icon):
    1. Mic captures audio → queue → full recording buffer
    2. No preview — just record cleanly

  When user stops (Ctrl+Shift+Space):
    1. Icon turns amber (transcribing)
    2. For short recordings (< batch threshold): transcribe all at once
    3. For long recordings: rolling batch chunks + tail
    4. Inject final text, icon returns to gray

  Cancel (Escape):
    1. Discard recording immediately
    2. Icon returns to gray, no transcription

  Mic only opens during active dictation. Zero resources when idle.
"""

import os
import queue
import threading
import time
import logging

import numpy as np
import sounddevice as sd

import config
import sounds

logger = logging.getLogger(__name__)


class AudioEngine:
    def __init__(self, transcriber, injector):
        self.transcriber = transcriber
        self.injector = injector

        self.on_state_change = None   # callback(state: str)
        self.on_title_update = None   # callback(title: str) — for recording timer
        self.on_final_text = None
        self._record_start_time = 0.0
        self._timer_thread: threading.Thread | None = None

        self._audio_queue: queue.Queue = queue.Queue(maxsize=500)
        self._active = threading.Event()
        self._running = threading.Event()
        self._finalizing = threading.Event()
        self._pipeline_thread: threading.Thread | None = None
        self._stream: sd.InputStream | None = None

        # Recording buffer
        self._full_recording: list[np.ndarray] = []

        # Rolling batch state (for long recordings)
        self._batch_results: list[str] = []
        self._batch_samples_done = 0
        self._batch_thread: threading.Thread | None = None
        self._batch_lock = threading.Lock()

    # ─── Lifecycle ───────────────────────────────────────────────

    def activate(self):
        if self._finalizing.is_set():
            logger.info("Still finalizing previous recording, ignoring activate")
            return

        self._full_recording.clear()
        self._batch_results.clear()
        self._batch_samples_done = 0
        self.injector.capture_target_window()
        self.injector.reset()

        self._running.set()
        self._active.set()

        self._stream = sd.InputStream(
            samplerate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            dtype=config.DTYPE,
            blocksize=config.BLOCK_SIZE,
            callback=self._audio_callback,
        )
        self._stream.start()

        self._pipeline_thread = threading.Thread(
            target=self._pipeline_loop,
            name="dictation-pipeline",
            daemon=True,
        )
        self._pipeline_thread.start()

        self._record_start_time = time.time()
        sounds.play_start()
        logger.info("Dictation ACTIVE (mic open)")
        if self.on_state_change:
            self.on_state_change("listening")

        # Start tooltip timer
        self._timer_thread = threading.Thread(
            target=self._timer_loop, daemon=True
        )
        self._timer_thread.start()

    def deactivate(self):
        if not self._active.is_set():
            return

        self._active.clear()
        self._running.clear()
        self._finalizing.set()

        sounds.play_stop()

        # Close mic immediately
        self._close_stream()

        # Show "transcribing" state immediately
        if self.on_state_change:
            self.on_state_change("transcribing")

        # Copy data for background finalization
        recording_data = list(self._full_recording)
        batch_results = list(self._batch_results)
        batch_done = self._batch_samples_done

        self._full_recording.clear()
        self._batch_results.clear()
        self._batch_samples_done = 0

        logger.info("Dictation STOPPED (mic closed, transcribing…)")

        def _do_finalize():
            try:
                self._finalize(recording_data, batch_results, batch_done)
            finally:
                self._finalizing.clear()
                if self.on_state_change:
                    self.on_state_change("idle")

        threading.Thread(target=_do_finalize, daemon=True).start()

    def cancel(self):
        """Cancel recording — discard audio, no transcription."""
        if not self._active.is_set():
            return

        self._active.clear()
        self._running.clear()

        sounds.play_cancel()

        # Close mic
        self._close_stream()

        # Discard everything
        self._full_recording.clear()
        self._batch_results.clear()
        self._batch_samples_done = 0
        self.injector.reset()

        logger.info("Dictation CANCELLED (recording discarded)")
        if self.on_state_change:
            self.on_state_change("idle")

    @property
    def is_active(self) -> bool:
        return self._active.is_set()

    def stop(self):
        if self._active.is_set():
            self.deactivate()

    # ─── Recording Timer ────────────────────────────────────────

    def _timer_loop(self):
        """Update tooltip with elapsed recording time every second."""
        while self._active.is_set():
            elapsed = time.time() - self._record_start_time
            mins, secs = divmod(int(elapsed), 60)
            title = f"Whisper Dictation — Listening… {mins}:{secs:02d}"
            if self.on_title_update:
                self.on_title_update(title)
            time.sleep(1.0)

    # ─── Stream Management ──────────────────────────────────────

    def _close_stream(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if self._pipeline_thread is not None:
            self._pipeline_thread.join(timeout=3)
            self._pipeline_thread = None

        if self._batch_thread is not None:
            self._batch_thread.join(timeout=15)
            self._batch_thread = None

    # ─── Audio Callback ─────────────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            logger.warning(f"Audio status: {status}")
        try:
            self._audio_queue.put_nowait(indata.copy())
        except queue.Full:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._audio_queue.put_nowait(indata.copy())
            except queue.Full:
                pass

    # ─── Pipeline (record-only) ─────────────────────────────────

    def _pipeline_loop(self):
        batch_interval_samples = int(config.BATCH_INTERVAL_S * config.SAMPLE_RATE)

        while self._running.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            # Record everything
            self._full_recording.append(chunk)

            # Check if time for a background batch (long recordings)
            total_samples = sum(len(c) for c in self._full_recording)
            unbatched = total_samples - self._batch_samples_done
            if unbatched >= batch_interval_samples:
                self._maybe_start_batch()

    # ─── Rolling Batch (for long recordings) ────────────────────

    def _maybe_start_batch(self):
        if self._batch_thread is not None and self._batch_thread.is_alive():
            return

        overlap_samples = int(config.BATCH_OVERLAP_S * config.SAMPLE_RATE)
        all_audio = np.concatenate(self._full_recording, axis=0).flatten()

        start_sample = max(0, self._batch_samples_done - overlap_samples)
        batch_end = len(all_audio)
        batch_audio = all_audio[start_sample:batch_end].copy()
        new_batch_done = batch_end

        if len(batch_audio) < config.SAMPLE_RATE:
            return

        def _run_batch():
            try:
                text = self.transcriber.transcribe(batch_audio)
                if text and text.strip():
                    with self._batch_lock:
                        self._batch_results.append(text.strip())
                    self._batch_samples_done = new_batch_done
                    logger.info(f"Batch complete: {len(self._batch_results)} chunks done")
            except Exception as e:
                logger.error(f"Batch error: {e}")

        self._batch_thread = threading.Thread(target=_run_batch, daemon=True)
        self._batch_thread.start()

    # ─── Finalize ────────────────────────────────────────────────

    def _finalize(self, recording_data, batch_results, batch_done):
        """Transcribe remaining audio and assemble final text."""
        if not recording_data:
            self.injector.reset()
            return

        all_audio = np.concatenate(recording_data, axis=0).flatten()
        total_duration = len(all_audio) / config.SAMPLE_RATE
        logger.info(f"Finalizing {total_duration:.1f}s recording")

        # Transcribe the tail (everything not yet batch-processed)
        overlap_samples = int(config.BATCH_OVERLAP_S * config.SAMPLE_RATE)
        start_sample = max(0, batch_done - overlap_samples)

        if start_sample < len(all_audio):
            tail_audio = all_audio[start_sample:]
            if len(tail_audio) >= config.SAMPLE_RATE * config.MIN_SPEECH_DURATION_S:
                try:
                    tail_text = self.transcriber.transcribe(tail_audio)
                    if tail_text and tail_text.strip():
                        batch_results.append(tail_text.strip())
                except Exception as e:
                    logger.error(f"Tail transcription error: {e}")

        final_text = " ".join(batch_results).strip()

        if final_text:
            logger.info(f"FINAL ({total_duration:.1f}s): {final_text}")
            self.injector.inject(final_text)
            sounds.play_complete()
            if self.on_final_text:
                self.on_final_text(final_text)
            if config.LOG_TRANSCRIPTIONS:
                self._log_transcription(final_text, total_duration)
        else:
            logger.info(f"No speech detected in {total_duration:.1f}s recording")
            self.injector.clear_interim()

    def _log_transcription(self, text: str, duration: float):
        try:
            log_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), config.LOG_FILE
            )
            self._rotate_log(log_path)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] ({duration:.1f}s) {text}\n")
        except Exception as e:
            logger.warning(f"Failed to write log: {e}")

    def _rotate_log(self, log_path: str):
        try:
            if not os.path.exists(log_path):
                return
            size = os.path.getsize(log_path)
            if size < config.LOG_MAX_BYTES:
                return
            rotated = log_path + ".1"
            if os.path.exists(rotated):
                os.remove(rotated)
            os.rename(log_path, rotated)
            logger.info(f"Log rotated ({size / 1024:.0f} KB)")
        except Exception as e:
            logger.warning(f"Log rotation failed: {e}")
