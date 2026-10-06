import os

import numpy as np

import lecture_store as store

SR = 16000


def _speech_with_gap(total_s, gap_at_s, gap_len_s=0.6):
    t = np.arange(int(total_s * SR)) / SR
    audio = 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32)
    a, b = int(gap_at_s * SR), int((gap_at_s + gap_len_s) * SR)
    audio[a:b] = 0.0
    return audio


def test_find_cut_point_lands_in_gap():
    audio = _speech_with_gap(62, 58.0)
    cut = store.find_cut_point(audio, SR, search_s=5)
    assert 58.0 * SR <= cut <= 58.6 * SR


def test_split_chunks_cover_everything_contiguously():
    rng = np.random.default_rng(0)
    audio = (0.2 * rng.standard_normal(SR * 200)).astype(np.float32)
    for gap in (57, 118, 176):
        audio[gap * SR:int((gap + 0.5) * SR)] = 0
    chunks = store.split_chunks(audio, SR, chunk_s=60, search_s=5)
    assert len(chunks) == 4
    pos = 0.0
    for offset, chunk in chunks:
        assert abs(offset - pos) < 1e-6
        pos += len(chunk) / SR
    assert abs(pos - 200) < 1e-6
    # cuts happened in the silent gaps
    assert 57 <= chunks[1][0] <= 57.5


def test_formatting():
    assert store.format_ts(3725.9) == "01:02:05"
    assert store.format_srt_ts(61.2345) == "00:01:01,234"
    assert store.format_duration(5472) == "1:31:12"
    assert store.format_duration(65) == "1:05"
    assert store.safe_name('Analysis: II / "Übung"?') == "Analysis II Übung"
    assert store.slug("Störungsbilder & Methoden") == "storungsbilder-methoden"


def test_session_lifecycle_and_transcript(tmp_path):
    root = str(tmp_path)
    cal = {"course": "M13 Methoden", "kind": "Seminar", "room": "R1.0 (Seminarraum)",
           "lecturer": "Erika Mustermann", "start": "2026-10-05T11:30", "end": "2026-10-05T13:00"}
    store.ensure_course(root, "M13 Methoden", "de")
    cdir = store.course_dir(root, "M13 Methoden")
    assert store.next_title(cdir, "de", "Seminar") == "Seminar 1"

    path = store.create_session(root, "M13 Methoden", "Seminar 1", "de", model="small", calendar=cal)
    meta = store.load_meta(path)
    assert meta["status"] == store.STATUS_RECORDING
    assert meta["lecturer"] == "Erika Mustermann" and meta["kind"] == "Seminar"
    assert store.next_title(cdir, "de", "Seminar") == "Seminar 2"
    assert store.next_title(cdir, "de") == "Vorlesung 2"
    assert store.find_incomplete(root) == [path]

    w = store.TranscriptWriter(path, paragraph_s=30)
    w.write_header(meta)
    w.add_bookmark(70.0)
    w.append_segments([(0.0, 5.0, "Guten Morgen."), (5.0, 12.0, "Heute: Regression."),
                       (31.0, 40.0, "Zweiter Absatz.")], offset_s=0.0)
    w.append_segments([(2.0, 9.0, "Nach der Pause.")], offset_s=65.0)
    w.append_note(130.0, "Transcription failed for this part.")
    w.finish()

    md = open(os.path.join(path, store.TRANSCRIPT_MD), encoding="utf-8").read()
    assert 'course: "M13 Methoden"' in md
    assert 'lecturer: "Erika Mustermann"' in md
    assert 'scheduled: "11:30–13:00"' in md
    assert "**[00:00:00]** Guten Morgen. Heute: Regression.\n" in md
    assert "**[00:00:31]** Zweiter Absatz." in md
    # the bookmark at 70 s comes after the paragraph that started at 67 s
    assert md.index("**[00:01:07]** Nach der Pause.") < md.index("🔖 **[00:01:10]**")
    assert "⚠ **[00:02:10]**" in md

    srt = open(os.path.join(path, store.TRANSCRIPT_SRT), encoding="utf-8").read()
    assert srt.startswith("1\n00:00:00,000 --> 00:00:05,000\nGuten Morgen.\n")
    assert "4\n00:01:07,000 --> 00:01:14,000\nNach der Pause." in srt

    # Search finds the hit with the paragraph's timestamp
    hits = store.search(root, "pause")
    assert len(hits) == 1 and hits[0]["timestamp"] == "00:01:07"
    assert store.search(root, "M13") == []  # front matter isn't searched

    # Finish, rename and move keep the transcript body intact
    meta["status"] = store.STATUS_COMPLETE
    meta["duration_s"] = 5472
    store.save_meta(path, meta)
    store.rewrite_front_matter(path, meta)
    assert store.find_incomplete(root) == []
    path = store.rename_session(path, "Seminar 1 – Regression")
    path = store.move_session(path, root, "M13 Multivariate Verfahren")
    md2 = open(os.path.join(path, store.TRANSCRIPT_MD), encoding="utf-8").read()
    assert "duration: 1:31:12" in md2
    assert 'title: "Seminar 1 – Regression"' in md2
    assert 'course: "M13 Multivariate Verfahren"' in md2
    assert md2.count("---\n") == 2 and md2.count("\n# ") == 1
    assert md2.endswith(md[md.index("**[00:00:00]**"):])
    assert [c["name"] for c in store.list_courses(root)] == ["M13 Methoden", "M13 Multivariate Verfahren"]


def test_wav_appender_survives_crash(tmp_path):
    p = str(tmp_path / "audio.wav")
    w = store.WavAppender(p, SR, header_every_s=1.0)
    w.write(np.full(SR * 3 + 100, 0.5, dtype=np.float32))
    w._f.flush()   # simulate a crash: never closed, header only updated periodically
    store.repair_wav(p)
    audio, sr = store.read_wav(p)
    assert sr == SR and len(audio) == SR * 3 + 100
    assert abs(audio[0] - 0.5) < 1e-3
    w.close()


def test_encode_mp3(tmp_path):
    wav = str(tmp_path / "a.wav")
    w = store.WavAppender(wav, SR)
    t = np.arange(SR * 2) / SR
    w.write((0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32))
    w.close()
    mp3 = str(tmp_path / "a.mp3")
    store.encode_mp3(wav, mp3, 64)
    assert os.path.getsize(mp3) > 1000
