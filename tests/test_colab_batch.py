"""Round trip: app export → Colab batch (fake engine) → import into the library."""

import io
import json
import os
import wave
import zipfile

import numpy as np

import colab_batch as cb
import colab_exchange as cx
import lecture_store as store
from test_course_calendar import TRAINEX_ICS

SR = 16000


class FakeEngine:
    name = "fake-large"

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, language, prompt):
        self.calls.append((len(audio) / SR, language, prompt))
        return [(0.0, 5.0, "Heute geht es um Skalenniveaus."),
                (31.0, 36.0, "Das ist so. Das ist so. Das ist so.")], language or "de"


def _tone(seconds):
    t = np.arange(int(seconds * SR)) / SR
    return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _wav_bytes(seconds):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(store.float_to_int16(_tone(seconds)).tobytes())
    return buf.getvalue()


def _mp3(path, seconds):
    wav = path + ".wav"
    with open(wav, "wb") as f:
        f.write(_wav_bytes(seconds))
    store.encode_mp3(wav, path)
    os.remove(wav)


def _app_lecture(root):
    path = store.create_session(root, "M13 I Multivariate Verfahren", "Seminar 1", "de")
    _mp3(os.path.join(path, store.AUDIO_MP3), 70)
    meta = store.load_meta(path)
    meta.update(status=store.STATUS_AUDIO_ONLY, duration_s=70.0,
                bookmarks=[{"t": 20.0, "label": "Bookmark"}])
    store.save_meta(path, meta)
    return path


def test_time_from_name():
    assert cb.time_from_name("Aufnahme 2026-10-06 at 13.52") == cb.datetime(2026, 10, 6, 13, 52)
    assert cb.time_from_name("20261006_135200") == cb.datetime(2026, 10, 6, 13, 52)
    assert cb.time_from_name("Vorlesung 06.10.2026 um 08:05") == cb.datetime(2026, 10, 6, 8, 5)
    assert cb.time_from_name("Neue Aufnahme 12") is None


def test_tag_times_become_local():
    assert cb._parse_tag_time("2026-10-06T11:52:00.000000Z") == cb.datetime(2026, 10, 6, 13, 52)
    assert cb._parse_tag_time("2026-10-06T13:52:00+0200") == cb.datetime(2026, 10, 6, 13, 52)
    assert cb._parse_tag_time("1904-01-01T00:00:00Z") is None


def test_round_trip(tmp_path):
    root = str(tmp_path / "Lectures")
    base = str(tmp_path / "Drive")
    inbox = os.path.join(base, "Inbox")
    os.makedirs(inbox)
    with open(os.path.join(base, "trainex.ics"), "w", encoding="utf-8") as f:
        f.write(TRAINEX_ICS)

    # 1. laptop recording, sent via "Send to Colab…"
    app_session = _app_lecture(root)
    assert cx.export_for_colab(root, os.path.join(inbox, "Colab-Upload.zip")) == 1
    # 2. GoodNotes mp3 export with the time in its name → M4 lecture at 08:00
    _mp3(os.path.join(inbox, "Aufnahme 2026-10-06 at 08.05.mp3"), 60)
    # 3. GoodNotes document: audio without extension, file date = end of the M8 VT
    gn = os.path.join(inbox, "Basiskompetenzen.goodnotes")
    with zipfile.ZipFile(gn, "w") as z:
        z.writestr(zipfile.ZipInfo("index.pb", (2026, 10, 1, 9, 0, 0)), b"\x08\x01")
        z.writestr(zipfile.ZipInfo("attachments/A1B2", (2026, 10, 6, 13, 50, 0)), _wav_bytes(60))
    # 4. no time anywhere → Unsorted
    _mp3(os.path.join(inbox, "Neue Aufnahme 12.mp3"), 45)
    # 5. accidental 5-second recording → skipped
    _mp3(os.path.join(inbox, "oops.mp3"), 5)

    engine = FakeEngine()
    log = []
    zip_path = cb.run(base, engine, log=log.append)
    assert zip_path and os.path.exists(zip_path)
    assert sorted(os.listdir(inbox)) == ["Done"]                       # all moved
    assert len(os.listdir(os.path.join(inbox, "Done"))) == 5
    assert all(lang == "de" for _, lang, _ in engine.calls)
    assert any(p and p.startswith("Vorlesung: M4") for _, _, p in engine.calls)

    z = zipfile.ZipFile(zip_path)
    names = z.namelist()
    # The zip is just transcripts + an overview (+ the app's import file)
    assert all(n.startswith("Transcripts/") or n in ("Overview.txt", cb.APP_IMPORT) for n in names)
    assert not any(n.endswith((".mp3", ".srt", ".json")) for n in names if n != cb.APP_IMPORT)
    overview = z.read("Overview.txt").decode("utf-8")
    assert "? Transcripts/Unsorted/" in overview
    m4_txt = next(n for n in names if n.startswith("Transcripts/M4 "))
    text = z.read(m4_txt).decode("utf-8").replace("\r\n", "\n")
    assert text.startswith("Vorlesung 06.10.\nM4 ")
    assert "[00:00]  Heute geht es um Skalenniveaus." in text and "★ [00:20]" not in text
    assert "---" not in text and "**" not in text
    course_by_title = {e["meta"]["title"]: e["meta"]["course"]
                       for e in json.loads(z.read(cb.APP_IMPORT))}
    assert course_by_title["Vorlesung 06.10."].startswith("M4 ")
    assert course_by_title["VT 06.10."].startswith("M8 ")
    assert course_by_title["Neue Aufnahme 12"] == cb.UNSORTED
    assert course_by_title["Seminar 1"].startswith("M13 ")
    assert len(course_by_title) == 4                             # oops.mp3 skipped

    # 6. import back into the library
    new, updated = cx.import_results(root, zip_path)
    assert (new, updated) == (3, 1)
    meta = store.load_meta(app_session)
    assert meta["transcribed_by"] == "colab" and meta["status"] == store.STATUS_COMPLETE
    assert meta["title"] == "Seminar 1" and meta["bookmarks"][0]["t"] == 20.0   # kept
    md = open(os.path.join(app_session, store.TRANSCRIPT_MD), encoding="utf-8").read()
    assert "Skalenniveaus" in md and md.count("Das ist so.") == 1 and "🔖" in md

    courses = {c["name"]: c for c in store.list_courses(root)}
    m4 = next(c for name, c in courses.items() if name.startswith("M4 "))
    (lec,) = store.list_sessions(m4["path"])
    assert lec["kind"] == "Vorlesung" and lec["lecturer"] == "Prof. Dr. Max Beispiel"
    assert store.find_audio(lec["path"]) is None                 # transcript only
    assert cb.UNSORTED in courses

    # nothing left to send, and an empty inbox does nothing
    assert cx.pending_sessions(root) == []
    assert cb.run(base, engine, log=log.append) is None
