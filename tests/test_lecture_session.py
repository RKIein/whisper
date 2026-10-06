import os
import threading
import time

import numpy as np

import config
import lecture
import lecture_store as store

SR = config.SAMPLE_RATE


class FakeTranscriber:
    model_id = "fake"

    def __init__(self, *args, **kwargs):
        self.calls = []

    def load(self, on_progress=None):
        pass

    def transcribe_segments(self, audio, language=None):
        dur = len(audio) / SR
        self.calls.append((dur, language))
        return [(0.0, dur, f"chunk of {dur:.1f} seconds")], "de"


class FakeStream:
    """Feeds a pre-made recording through the callback like sounddevice would."""

    def __init__(self, audio, callback, block=2048):
        self.audio, self.callback, self.block = audio, callback, block
        self.done = threading.Event()
        self._stop = threading.Event()

    def start(self):
        def run():
            for i in range(0, len(self.audio), self.block):
                if self._stop.is_set():
                    break
                self.callback(self.audio[i:i + self.block].reshape(-1, 1), self.block, None, None)
                time.sleep(0.0005)
            self.done.set()
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def close(self):
        pass


def _lecture_audio(seconds):
    rng = np.random.default_rng(1)
    audio = (0.2 * rng.standard_normal(SR * seconds)).astype(np.float32)
    for gap in range(7, seconds, 9):           # a short pause every 9 s
        audio[gap * SR:int((gap + 0.4) * SR)] = 0
    return audio


def test_full_lecture_session(tmp_path):
    audio = _lecture_audio(150)
    fake = FakeTranscriber()
    session = lecture.LectureSession(fake, str(tmp_path))
    states = []
    finished = threading.Event()
    session.on_state_change = states.append
    session.on_finished = lambda d, m: finished.set()

    streams = []

    def factory(cb):
        streams.append(FakeStream(audio, cb))
        return streams[0]

    cal = {"kind": "Vorlesung", "lecturer": "Prof. Dr. Max Beispiel", "room": "R0.0",
           "start": "2026-10-06T08:00", "end": "2026-10-06T09:30"}
    session.start("M4 Dokumentation", "Vorlesung 1", "de", calendar=cal, input_stream_factory=factory)
    assert streams[0].done.wait(10)
    time.sleep(0.3)             # let the pipeline drain the audio queue
    session.bookmark()          # at the end of the recording
    session.stop()
    assert finished.wait(10)

    path = session.session_dir
    meta = store.load_meta(path)
    assert meta["status"] == store.STATUS_COMPLETE
    assert abs(meta["duration_s"] - 150) < 0.5
    assert meta["detected_languages"] == ["de"]
    assert meta["lecturer"] == "Prof. Dr. Max Beispiel"
    assert len(meta["bookmarks"]) == 1
    assert states[0] == lecture.STATE_LECTURE and states[-2:] == [lecture.STATE_LECTURE_FINISHING, "idle"]

    # chunks: ~60 s each, cut at pauses, covering the whole recording
    durations = [d for d, _ in fake.calls]
    assert len(durations) == 3
    assert all(50 <= d <= 70 for d in durations[:2])
    assert abs(sum(durations) - 150) < 0.5
    assert all(lang == "de" for _, lang in fake.calls)

    md = open(os.path.join(path, store.TRANSCRIPT_MD), encoding="utf-8").read()
    assert md.count("chunk of") == 3
    assert "🔖 **[00:02:30]**" in md
    assert "duration: 2:30" in md

    assert os.path.exists(os.path.join(path, store.AUDIO_MP3))
    assert not os.path.exists(os.path.join(path, store.AUDIO_WAV))


def test_pause_skips_audio(tmp_path):
    audio = _lecture_audio(20)
    session = lecture.LectureSession(FakeTranscriber(), str(tmp_path))
    holder = {}

    def factory(cb):
        holder["cb"] = cb
        class S:
            def start(self): pass
            def stop(self): pass
            def close(self): pass
        return S()

    session.start("Kurs", "Test", "auto", input_stream_factory=factory)
    cb = holder["cb"]
    half = len(audio) // 2
    for i in range(0, half, 2048):
        cb(audio[i:i + 2048].reshape(-1, 1), 2048, None, None)
    time.sleep(0.3)
    session.pause()
    time.sleep(0.1)
    for i in range(half, len(audio), 2048):
        cb(audio[i:i + 2048].reshape(-1, 1), 2048, None, None)
    time.sleep(0.3)
    session.stop()
    assert session.wait_finished(10)
    meta = store.load_meta(session.session_dir)
    assert abs(meta["duration_s"] - 10) < 0.5


def test_mic_failure_leaves_no_session(tmp_path):
    session = lecture.LectureSession(FakeTranscriber(), str(tmp_path))

    def factory(cb):
        raise OSError("no input device")

    try:
        session.start("Kurs", "Test", "de", input_stream_factory=factory)
        assert False, "should raise"
    except OSError:
        pass
    assert not session.is_active
    assert store.list_sessions(store.course_dir(str(tmp_path), "Kurs")) == []


def test_retranscribe_crashed_session(tmp_path, monkeypatch):
    import transcriber
    monkeypatch.setattr(transcriber, "Transcriber", FakeTranscriber)

    root = str(tmp_path)
    path = store.create_session(root, "Kurs", "Vorlesung 1", "de")
    meta = store.load_meta(path)
    meta["bookmarks"] = [{"t": 30.0, "label": "Bookmark"}]
    store.save_meta(path, meta)
    w = store.WavAppender(os.path.join(path, store.AUDIO_WAV), SR, header_every_s=1000)
    w.write(_lecture_audio(130))
    w._f.flush()                 # crash: header still says 0 frames
    w._f.close()

    meta = lecture.retranscribe(path, "fake")
    assert meta["status"] == store.STATUS_COMPLETE
    assert abs(meta["duration_s"] - 130) < 0.5
    md = open(os.path.join(path, store.TRANSCRIPT_MD), encoding="utf-8").read()
    assert md.count("chunk of") == 3 and "🔖 **[00:00:30]**" in md
    assert os.path.exists(os.path.join(path, store.AUDIO_MP3))
