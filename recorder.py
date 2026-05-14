"""
Voice Recorder — saves audio to file instead of transcribing.

Records from the mic using sounddevice and saves in the user's
chosen format. Runs silently in the background with minimal UI —
just a tray state change and a quiet notification when done.

Supported formats:
  WAV  — lossless, no extra dependencies, large files
  MP3  — universal compatibility, uses lameenc (no ffmpeg needed)
  OGG  — good quality/size, requires pydub + ffmpeg
"""

import logging
import os
import queue
import threading
import time

import numpy as np
import sounddevice as sd

import config
import sounds

logger = logging.getLogger(__name__)

RECORDING_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Whisper Recordings")

FORMATS = {
    "wav":  {"label": "WAV",      "ext": ".wav"},
    "mp3":  {"label": "MP3",      "ext": ".mp3"},
    "ogg":  {"label": "OGG Opus", "ext": ".ogg"},
}

DEFAULT_FORMAT = "mp3"

RECORD_SAMPLE_RATE = 44100
RECORD_CHANNELS = 1
RECORD_BLOCK_SIZE = 1024


class VoiceRecorder:

    def __init__(self):
        self._audio_queue: queue.Queue = queue.Queue(maxsize=2000)
        self._recording_data: list[np.ndarray] = []
        self._active = threading.Event()
        self._stream: sd.InputStream | None = None
        self._pipeline_thread: threading.Thread | None = None
        self._start_time = 0.0
        self._format = DEFAULT_FORMAT

        self.on_state_change = None
        self.on_title_update = None
        self.on_recording_saved = None

    @property
    def is_active(self) -> bool:
        return self._active.is_set()

    @property
    def format(self) -> str:
        return self._format

    @format.setter
    def format(self, fmt: str):
        if fmt in FORMATS:
            self._format = fmt
            logger.info(f"Recording format: {fmt}")

    def start(self):
        if self._active.is_set():
            return

        os.makedirs(RECORDING_DIR, exist_ok=True)

        self._recording_data.clear()
        self._active.set()

        self._stream = sd.InputStream(
            samplerate=RECORD_SAMPLE_RATE,
            channels=RECORD_CHANNELS,
            dtype="float32",
            blocksize=RECORD_BLOCK_SIZE,
            callback=self._audio_callback,
        )
        self._stream.start()

        self._pipeline_thread = threading.Thread(
            target=self._pipeline_loop, daemon=True
        )
        self._pipeline_thread.start()

        self._start_time = time.time()
        sounds.play_start()
        logger.info("Voice recording STARTED")

        if self.on_state_change:
            self.on_state_change("recording")

        threading.Thread(target=self._timer_loop, daemon=True).start()

    def stop(self):
        if not self._active.is_set():
            return

        self._active.clear()
        sounds.play_stop()

        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if self._pipeline_thread is not None:
            self._pipeline_thread.join(timeout=3)
            self._pipeline_thread = None

        recording_data = list(self._recording_data)
        self._recording_data.clear()

        if self.on_state_change:
            self.on_state_change("saving")

        threading.Thread(
            target=self._save, args=(recording_data,), daemon=True
        ).start()

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            logger.warning(f"Recording audio status: {status}")
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

    def _pipeline_loop(self):
        while self._active.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._recording_data.append(chunk)

    def _timer_loop(self):
        while self._active.is_set():
            elapsed = time.time() - self._start_time
            mins, secs = divmod(int(elapsed), 60)
            title = f"Whisper Dictation — Recording… {mins}:{secs:02d}"
            if self.on_title_update:
                self.on_title_update(title)
            time.sleep(1.0)

    def _save(self, recording_data: list[np.ndarray]):
        if not recording_data:
            logger.info("No audio recorded")
            if self.on_state_change:
                self.on_state_change("idle")
            return

        try:
            audio = np.concatenate(recording_data, axis=0)
            duration = len(audio) / RECORD_SAMPLE_RATE
            timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            fmt_info = FORMATS[self._format]
            filename = f"recording_{timestamp}{fmt_info['ext']}"
            filepath = os.path.join(RECORDING_DIR, filename)

            if self._format == "mp3":
                self._save_mp3(filepath, audio)
            elif self._format == "ogg":
                self._save_ogg(filepath, audio)
            else:
                self._save_wav(filepath, audio)

            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            logger.info(
                f"Recording saved: {filename} "
                f"({duration:.1f}s, {size_mb:.1f} MB)"
            )
            sounds.play_complete()

            if self.on_recording_saved:
                self.on_recording_saved(filepath, duration)

        except Exception as e:
            logger.error(f"Failed to save recording: {e}")
        finally:
            if self.on_state_change:
                self.on_state_change("idle")

    def _to_int16(self, audio: np.ndarray) -> np.ndarray:
        audio_int16 = (audio * 32767).astype(np.int16)
        if audio_int16.ndim > 1:
            audio_int16 = audio_int16[:, 0]
        return audio_int16

    def _save_wav(self, filepath: str, audio: np.ndarray):
        import wave
        pcm = self._to_int16(audio)
        with wave.open(filepath, "wb") as wf:
            wf.setnchannels(RECORD_CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(RECORD_SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())

    def _save_mp3(self, filepath: str, audio: np.ndarray):
        import lameenc

        pcm = self._to_int16(audio)
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(192)
        encoder.set_in_sample_rate(RECORD_SAMPLE_RATE)
        encoder.set_channels(RECORD_CHANNELS)
        encoder.set_quality(2)

        mp3_data = encoder.encode(pcm.tobytes())
        mp3_data += encoder.flush()

        with open(filepath, "wb") as f:
            f.write(mp3_data)

    def _save_ogg(self, filepath: str, audio: np.ndarray):
        from pydub import AudioSegment

        pcm = self._to_int16(audio)
        segment = AudioSegment(
            data=pcm.tobytes(),
            sample_width=2,
            frame_rate=RECORD_SAMPLE_RATE,
            channels=RECORD_CHANNELS,
        )
        segment.export(filepath, format="ogg", codec="libopus", bitrate="128k")
