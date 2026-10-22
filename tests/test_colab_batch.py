"""Colab batch: GoodNotes backups + Inbox → sorted transcripts (fake engine), app import, Notion."""

import io
import json
import os
import wave
import zipfile

import numpy as np

import colab_batch as cb
import colab_exchange as cx
import lecture_store as store
import notion_export as nx
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


def _tone(seconds, hz=220):
    t = np.arange(int(seconds * SR)) / SR
    return (0.2 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def _wav_bytes(seconds, hz=220):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(store.float_to_int16(_tone(seconds, hz)).tobytes())
    return buf.getvalue()


def _mp3(path, seconds):
    with open(path + ".wav", "wb") as f:
        f.write(_wav_bytes(seconds))
    store.encode_mp3(path + ".wav", path)
    os.remove(path + ".wav")


def _goodnotes(path, recordings):
    """A GoodNotes-like zip: page data plus audio files without extensions.
    recordings: [(member name, seconds, end time tuple, tone hz)]"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("index.notes.pb", (2026, 9, 1, 9, 0, 0)), b"\x08\x01" * 10)
        z.writestr(zipfile.ZipInfo("attachments/PDF-1", (2026, 9, 1, 9, 0, 0)), b"%PDF-1.7" + b"0" * 60000)
        for name, seconds, end, hz in recordings:
            z.writestr(zipfile.ZipInfo(f"attachments/{name}", end), _wav_bytes(seconds, hz))


def test_time_from_name():
    assert cb.time_from_name("Aufnahme 2026-10-06 at 13.52") == cb.datetime(2026, 10, 6, 13, 52)
    assert cb.time_from_name("20261006_135200") == cb.datetime(2026, 10, 6, 13, 52)
    assert cb.time_from_name("Vorlesung 06.10.2026 um 08:05") == cb.datetime(2026, 10, 6, 8, 5)
    assert cb.time_from_name("Neue Aufnahme 12") is None


def test_tag_times_become_local():
    assert cb._parse_tag_time("2026-10-06T11:52:00.000000Z") == cb.datetime(2026, 10, 6, 13, 52)
    assert cb._parse_tag_time("2026-10-06T13:52:00+0200") == cb.datetime(2026, 10, 6, 13, 52)
    assert cb._parse_tag_time("1904-01-01T00:00:00Z") is None


def test_course_hint_from_notebook_name():
    events = cb.course_calendar.parse_ics(TRAINEX_ICS)
    assert cb.course_hint("M13 Notizen WS26", events).startswith("M13 I Multivariate")
    assert cb.course_hint("m4 Doku", events).startswith("M4 Dokumentation")
    assert cb.course_hint("M130 something", events) is None
    assert cb.course_hint("Mathe", events) is None
    # Robin's real GoodNotes layout: the course folder decides, the notebook name is the fallback
    assert cb.course_hint("M. Sc. Psychotherapie/M13 Multivariate Verfahren - Forschungsmethoden/"
                          "3_Replikation_v17071.pdf", events).startswith("M13 I")
    assert cb.course_hint("M. Sc. Psychotherapie/M4 Qualitässicherung/M4_M6_Termin_2",
                          events).startswith("M4 ")
    assert cb.course_hint("M. Sc. Psychotherapie/M5 Testkonstruktion/VL_DMM_01_-_Einfuehrung",
                          events).startswith("M5 ")
    assert cb.course_hint("Studium/M8 Basiskompetenzen", events).startswith("M8 ")


def test_goodnotes_times_are_end_times():
    """Two back-to-back GoodNotes recordings in one lecture (pattern from a real preview):
    stamped 08:50 (50 min) and 09:31 (35 min) — they only fit 08:00–09:30 as end times."""
    events = cb.course_calendar.parse_ics(TRAINEX_ICS)      # M4 Vorlesung 06.10. 08:00–09:30
    doc = "M. Sc. Psychotherapie/M4 Qualitässicherung/M4_M6_Termin_2"
    plans = [cb.plan(cb.Recording(source="x", key=k, open=None, label="x", doc=doc,
                                  time=cb.datetime(2026, 10, 6, h, m), time_is_end=True,
                                  time_source="GoodNotes, end of recording", duration_s=mins * 60),
                     events)
             for k, h, m, mins in (("a", 8, 50, 50), ("b", 9, 31, 35))]
    assert [p["meta"]["started"][11:16] for p in plans] == ["08:00", "08:56"]
    assert {p["group"] for p in plans} == {plans[0]["group"]}           # → one transcript
    assert plans[0]["meta"]["title"] == "Vorlesung 06.10." and plans[0]["meta"]["kind"] == "Vorlesung"


BLOCK_DAY_ICS = "BEGIN:VCALENDAR\r\n" + "".join(
    "BEGIN:VEVENT\r\nSUMMARY:M13 I Multivariate Verfahren/Vorlesung - R0.0 (Hörsaal) - x\r\n"
    f"DTSTART:20261009T{a}00\r\nDTEND:20261009T{b}00\r\nEND:VEVENT\r\n"
    for a, b in (("0945", "1115"), ("1130", "1300"))) + "END:VCALENDAR\r\n"


def test_block_day_groups_by_notebook():
    """Robin's real 09.10.: the Replikation deck started at 10:55, before the 11:30 slot.
    Its two recordings belong together — not with the Wiederholung deck of the 09:45 slot."""
    events = cb.course_calendar.parse_ics(BLOCK_DAY_ICS)
    folder = "M. Sc. Psychotherapie/M13 Multivariate Verfahren - Forschungsmethoden/"

    def p(key, doc, h, m, mins):
        return cb.plan(cb.Recording(source=key, key=key, open=None, label=key, doc=folder + doc,
                                    time=cb.datetime(2026, 10, 9, h, m), time_is_end=True,
                                    duration_s=mins * 60), events)
    w1, w2 = p("w1", "2_Wiederholung_v06085.pdf", 10, 17, 36), p("w2", "2_Wiederholung_v06085.pdf", 10, 28, 11)
    r1, r2 = p("r1", "3_Replikation_v17071.pdf", 11, 19, 24), p("r2", "3_Replikation_v17071.pdf", 12, 31, 61)
    assert w1["group"] == w2["group"] and r1["group"] == r2["group"] != w1["group"]
    assert r1["meta"]["title"] == "Vorlesung 09.10. – Replikation"
    results = [{"group": x["group"], "meta": x["meta"], "transcript_md": ""} for x in (r2, w1, r1, w2)]
    assert [[r["meta"]["started"][11:16] for r in g] for g in cb.lectures(results)] == \
        [["09:41", "10:17"], ["10:55", "11:30"]]                       # parts in time order


def test_title_umlauts_from_slides():
    slides = "Einführung und Grundlagen\nEffektgrößen"
    assert cb.fix_umlauts("Einfuehrung und Grundlagen", slides) == "Einführung und Grundlagen"
    assert cb.fix_umlauts("Effektgroessen", slides) == "Effektgrößen"
    assert cb.fix_umlauts("neue aktuelle Quellen", slides) == "neue aktuelle Quellen"
    # Real M13 deck: the title word only appears as a heading in capitals
    assert cb.fix_umlauts("Effektgroessen", "EFFEKTGRÖßEN\nEffektgröße") == "Effektgrößen"


def test_topic_from_notebook_name():
    assert cb.topic("M13 …/3_Replikation_v17071.pdf") == "Replikation"
    assert cb.topic("M13 …/5_Datenqualitaet_v30439 2") == "Datenqualitaet"
    assert cb.topic("VL_DMM_02_-_Itemanalyse_und_Exploratorische_Faktorenanalyse") \
        == "Itemanalyse und Exploratorische Faktorenanalyse"
    assert cb.topic("1_Warm_Up_und_Agenda_v88894.pdf") == "Warm Up und Agenda"
    assert cb.topic("M4_M6_Termin_2") == "" and cb.topic("M1_2_IH") == "" and cb.topic("") == ""
    assert cb.topic("M8 Basiskompetenzen", "M8 Psychotherapeutische Basiskompetenzen und …") == ""


def test_preview_changes_nothing(tmp_path):
    base = str(tmp_path / "Vorlesungen")
    backup = tmp_path / "GoodNotes" / "M. Sc. Psychotherapie" / "M5 Testkonstruktion"
    os.makedirs(os.path.join(base, "Inbox"))
    os.makedirs(backup)
    with open(os.path.join(base, "t.ics"), "w", encoding="utf-8") as f:
        f.write(TRAINEX_ICS)
    _goodnotes(str(backup / "VL_DMM_01.goodnotes"), [("R1", 60, (2026, 10, 6, 16, 20, 0), 220)])
    with zipfile.ZipFile(str(backup / "VL_DMM_02.goodnotes"), "w") as z:      # slides only
        z.writestr("attachments/PDF", b"%PDF-1.7" + b"0" * 60000)
    _mp3(os.path.join(base, "Inbox", "x.mp3"), 40)
    res = cb.run(base, None, backup_folders=[str(tmp_path / "GoodNotes")], preview=True,
                 log=lambda *_: None)
    text = open(res["preview"], encoding="utf-8").read()
    assert "VL_DMM_01 › R1" in text and "→ M5 Diagnostische Modelle" in text
    assert "folders" not in text and "VL_DMM_02" in text.split("without new recordings")[1]
    assert os.listdir(os.path.join(base, "Inbox")) == ["Done", "x.mp3"]   # nothing moved
    assert not os.path.exists(os.path.join(base, ".state", "processed.json"))
    assert not os.path.exists(os.path.join(base, "Transkripte"))


def test_semester_flow(tmp_path):
    root = str(tmp_path / "Lectures")             # the laptop app's library
    drive = tmp_path / "MyDrive"
    base = str(drive / "Vorlesungen")
    backup = drive / "Goodnotes"
    os.makedirs(os.path.join(base, "Inbox"))
    os.makedirs(backup / "Studium")
    with open(os.path.join(base, "trainex.ics"), "w", encoding="utf-8") as f:
        f.write(TRAINEX_ICS)
    assert cb.find_goodnotes_backup(str(drive)) == [str(backup)]

    # GoodNotes auto-backup. M8 VT on 06.10. 12:30–14:00, recorded in two parts (paused),
    # plus an M5 seminar recording (15:30–17:00) sitting in a notebook named after M5.
    m8 = str(backup / "Studium" / "M8 Basiskompetenzen.goodnotes")
    _goodnotes(m8, [("A1", 60, (2026, 10, 6, 13, 10, 0), 220),
                    ("A2", 60, (2026, 10, 6, 13, 55, 0), 330)])
    m5 = str(backup / "Studium" / "M5 Diagnostik.goodnotes")
    _goodnotes(m5, [("B1", 60, (2026, 10, 6, 16, 30, 0), 440)])
    # Inbox: a laptop lecture via “Send to Colab”, a GoodNotes mp3 export with the time
    # in its name (M4 lecture at 08:00), an undated file and an accidental tap.
    app_session = store.create_session(root, "M13 I Multivariate Verfahren", "Seminar 1", "de")
    _mp3(os.path.join(app_session, store.AUDIO_MP3), 70)
    meta = store.load_meta(app_session)
    meta.update(status=store.STATUS_AUDIO_ONLY, duration_s=70.0,
                bookmarks=[{"t": 20.0, "label": "Bookmark"}])
    store.save_meta(app_session, meta)
    inbox = os.path.join(base, "Inbox")
    assert cx.export_for_colab(root, os.path.join(inbox, "Colab-Upload.zip")) == 1
    _mp3(os.path.join(inbox, "Aufnahme 2026-10-06 at 08.05.mp3"), 60)
    _mp3(os.path.join(inbox, "Neue Aufnahme 12.mp3"), 45)
    _mp3(os.path.join(inbox, "oops.mp3"), 5)

    engine = FakeEngine()
    published = []
    res = cb.run(base, engine, backup_folders=cb.find_goodnotes_backup(str(drive)),
                 publish=published.append, log=lambda *_: None)

    # Inbox emptied, backups untouched
    assert sorted(os.listdir(inbox)) == ["Done"] and len(os.listdir(os.path.join(inbox, "Done"))) == 4
    assert os.path.exists(m8) and os.path.exists(m5)
    # Transcripts straight into Drive, one per lecture
    out = os.path.join(base, "Transkripte")
    files = sorted(os.path.relpath(os.path.join(r, f), out).replace(os.sep, "/")
                   for r, _, fs in os.walk(out) for f in fs)
    assert len(files) == 5, files                  # M8 (2 parts → 1), M5, M4, M13, Unsorted
    m8_txt = next(f for f in files if f.startswith("M8 "))
    assert m8_txt.endswith("2026-10-06 – VT 06.10.txt")
    text = open(os.path.join(out, m8_txt), encoding="utf-8").read()
    assert text.count("— Part") == 2 and text.count("Skalenniveaus") == 2
    assert text.count("Das ist so.") == 2          # repetition loops collapsed per part
    assert any(f.startswith("M5 ") for f in files)  # notebook name + timetable
    assert any(f.startswith("M4 ") and "Vorlesung 06.10." in f for f in files)
    assert any(f.startswith("Unsorted/") for f in files)
    assert res["unsorted"] == 1 and res["failed"] == 0
    assert all(p is None for _, _, p in engine.calls)   # no prompt: large models echo it
    assert len(published) == 1 and len(published[0]) == 5

    # Results zip: transcripts + app import; laptop import updates/adds
    z = zipfile.ZipFile(res["zip"])
    assert all(n.startswith("Transcripts/") or n == cb.APP_IMPORT for n in z.namelist())
    new, updated = cx.import_results(root, res["zip"])
    assert (new, updated) == (5, 1)                # each GoodNotes part is its own app lecture
    meta = store.load_meta(app_session)
    assert meta["transcribed_by"] == "colab" and meta["bookmarks"][0]["t"] == 20.0

    # Next week: the M8 notebook has grown by one recording. Only that one is transcribed.
    _goodnotes(m8, [("A1", 60, (2026, 10, 6, 13, 10, 0), 220),
                    ("A2", 60, (2026, 10, 6, 13, 55, 0), 330),
                    ("A3", 60, (2026, 10, 13, 13, 50, 0), 550)])
    engine.calls.clear()
    res2 = cb.run(base, engine, backup_folders=[str(backup)], log=lambda *_: None)
    assert len(engine.calls) == 1 and len(res2["transcripts"]) == 1
    assert "2026-10-13" in res2["transcripts"][0]
    # Nothing new → nothing happens
    assert cb.run(base, engine, backup_folders=[str(backup)], log=lambda *_: None)["zip"] is None


def test_old_semesters_in_backup_are_skipped(tmp_path):
    base = str(tmp_path / "Vorlesungen")
    backup = tmp_path / "Goodnotes"
    os.makedirs(os.path.join(base, "Inbox"))
    with open(os.path.join(base, "t.ics"), "w", encoding="utf-8") as f:
        f.write(TRAINEX_ICS)                               # semester starts 05.10.2026
    os.makedirs(backup / "Bachelor")
    os.makedirs(backup / "Master")
    # Bachelor: a dated recording from 2024, and an undated one (all files stamped alike)
    _goodnotes(str(backup / "Bachelor" / "Statistik II.goodnotes"),
               [("OLD1", 60, (2024, 5, 14, 11, 0, 0), 220)])
    with zipfile.ZipFile(str(backup / "Bachelor" / "Sozialpsychologie.goodnotes"), "w") as z:
        for name, data in (("index.notes.pb", b"x" * 100), ("attachments/OLD2", _wav_bytes(60, 660))):
            z.writestr(zipfile.ZipInfo(name, (2026, 10, 20, 9, 0, 0)), data)
    # Master: this semester
    _goodnotes(str(backup / "Master" / "M4 Doku.goodnotes"), [("NEW1", 60, (2026, 10, 6, 9, 25, 0), 330)])

    engine = FakeEngine()
    res = cb.run(base, engine, backup_folders=[str(backup)], log=lambda *_: None)
    assert len(engine.calls) == 1 and len(res["transcripts"]) == 1      # only the M4 recording
    processed = json.load(open(os.path.join(base, ".state", "processed.json"), encoding="utf-8"))
    assert sum(v.get("skipped") == "before this semester" for v in processed.values()) == 2
    # Next run: nothing to unpack or transcribe again
    engine.calls.clear()
    assert cb.run(base, engine, backup_folders=[str(backup)], log=lambda *_: None)["zip"] is None
    assert engine.calls == []
    # Folder filter
    assert [os.path.basename(p) for p in cb.backup_files([str(backup)], only="master")] == ["M4 Doku.goodnotes"]


def test_resume_after_disconnect(tmp_path):
    base = str(tmp_path / "Vorlesungen")
    os.makedirs(os.path.join(base, "Inbox"))
    with open(os.path.join(base, "t.ics"), "w", encoding="utf-8") as f:
        f.write(TRAINEX_ICS)
    _goodnotes(os.path.join(base, "Inbox", "M4 Doku.goodnotes"),
               [("C1", 60, (2026, 10, 6, 9, 20, 0), 220), ("C2", 60, (2026, 10, 6, 15, 59, 0), 300)])

    class Dies(FakeEngine):
        def transcribe(self, audio, language, prompt):
            if self.calls:
                raise KeyboardInterrupt            # Colab disconnects during the 2nd recording
            return super().transcribe(audio, language, prompt)

    try:
        cb.run(base, Dies(), log=lambda *_: None)
    except KeyboardInterrupt:
        pass
    assert os.path.exists(os.path.join(base, "Inbox", "M4 Doku.goodnotes"))   # not done yet
    engine = FakeEngine()
    res = cb.run(base, engine, log=lambda *_: None)
    assert len(engine.calls) == 1                  # only the unfinished one
    assert len(res["transcripts"]) == 2


def test_results_saved_before_per_notebook_groups_are_regrouped():
    """Lectures transcribed by the previous version keep their text; only the group changes."""
    old = {"key": "k", "source": "M. Sc. Psychotherapie/M13 …/3_Replikation_v17071.pdf › 0E27",
           "group": "M13 I Multivariate Verfahren|2026-10-09T09:45",
           "meta": {"origin": "upload", "course": "M13 I Multivariate Verfahren",
                    "started": "2026-10-09T10:55:00"}}
    assert cb.regroup(old) == "M13 I Multivariate Verfahren|M. Sc. Psychotherapie/M13 …/3_Replikation_v17071.pdf|2026-10-09"
    app = dict(old, source="Lectures.zip › 2026-10-06 13-52 – Seminar 1", group="app:x",
               meta=dict(old["meta"], origin="app"))
    assert cb.regroup(app) == "app:x"
    unsorted = dict(old, group="rec:k", meta=dict(old["meta"], course=cb.UNSORTED))
    assert cb.regroup(unsorted) == "rec:k"


def test_setup_error_stops_early(tmp_path):
    base = str(tmp_path / "Vorlesungen")
    os.makedirs(os.path.join(base, "Inbox"))
    _goodnotes(os.path.join(base, "Inbox", "M4 Doku.goodnotes"),
               [(f"C{i}", 60, (2026, 10, 6, 9 + i, 20, 0), 200 + 50 * i) for i in range(5)])

    class Broken(FakeEngine):
        def transcribe(self, audio, language, prompt):
            self.calls.append(1)
            raise RuntimeError("Library libcublas.so.12 is not found or cannot be loaded")

    engine = Broken()
    res = cb.run(base, engine, log=lambda *_: None)
    assert len(engine.calls) == 3 and res["failed"] == 3
    assert os.path.exists(os.path.join(base, "Inbox", "M4 Doku.goodnotes"))
    res = cb.run(base, FakeEngine(), log=lambda *_: None)    # fixed setup → all five still there
    assert res["failed"] == 0 and len(res["transcripts"]) == 5


class FakeNotion:
    """Records requests; behaves like the Notion API for what notion_export uses."""

    def __init__(self):
        self.pages, self.appends, self.dbs = [], [], []

    def request(self, method, url, headers, json, timeout):
        path = url.split("/v1", 1)[1]

        class R:
            status_code, headers_ = 200, {}

            def __init__(self, data):
                self._d = data
                self.headers = {}

            def json(self):
                return self._d
        if method == "POST" and path == "/databases":
            self.dbs.append(json)
            return R({"id": "db1"})
        if method == "POST" and path.endswith("/query"):
            key = json["filter"]["rich_text"]["equals"]
            hits = [p for p in self.pages if p["properties"]["Schlüssel"]["rich_text"][0]["text"]["content"] == key]
            return R({"results": hits})
        if method == "POST" and path == "/pages":
            self.pages.append(json)
            return R({"id": f"page{len(self.pages)}"})
        if method == "PATCH":
            self.appends.append(json)
            return R({})
        raise AssertionError(path)


def test_notion_publish():
    assert nx.page_id_from_url("https://www.notion.so/Vorlesungen-1a2b3c4d5e6f40718293a4b5c6d7e8f9?pvs=4") \
        == "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"
    md = "\n".join(f"**[00:{m:02d}:00]** Absatz {m} " + "x" * 2500 for m in range(60)) + "\n"
    part = {"group": "M13 I Multivariate Verfahren, Forschung|2026-10-06T13:45", "key": "z:1",
            "meta": {"title": "Seminar 06.10.", "course": "M13 I Multivariate Verfahren, Forschung",
                     "kind": "Seminar", "lecturer": "Carolin Hey", "started": "2026-10-06T13:52:00",
                     "duration_s": 3600}, "transcript_md": md}
    fake = FakeNotion()
    notion = nx.Notion("secret", session=fake)
    db = nx.ensure_database(notion, "parent-page")
    assert nx.publish([[part, dict(part, transcript_md=md)]], notion, db, cb.lecture_text) == 1
    page = fake.pages[0]
    assert page["properties"]["Kurs"]["select"]["name"] == "M13 I Multivariate Verfahren · Forschung"
    assert page["properties"]["Dauer (min)"]["number"] == 120
    body = page["children"] + [b for a in fake.appends for b in a["children"]]
    assert len(page["children"]) == 100 and sum(b["type"] == "heading_3" for b in body) == 2
    assert all(len(t["text"]["content"]) <= 2000 for b in body for t in b[b["type"]]["rich_text"])
    # Second run: already there → not added again
    assert nx.publish([[part]], notion, db, cb.lecture_text) == 0


def test_transcribe_again_replaces_old_files(tmp_path):
    base = str(tmp_path / "Vorlesungen")
    backup = tmp_path / "Goodnotes" / "Master"
    os.makedirs(os.path.join(base, "Inbox"))
    os.makedirs(backup)
    with open(os.path.join(base, "t.ics"), "w", encoding="utf-8") as f:
        f.write(TRAINEX_ICS)
    _goodnotes(str(backup / "M4 Doku.goodnotes"),
               [("P1", 60, (2026, 10, 6, 8, 50, 0), 220), ("P2", 60, (2026, 10, 6, 9, 20, 0), 330)])
    first = cb.run(base, FakeEngine(), backup_folders=[str(backup.parent)], log=lambda *_: None)
    assert len(first["transcripts"]) == 1                     # two parts, one lecture

    class Better(FakeEngine):
        def transcribe(self, audio, language, prompt):
            self.calls.append(1)
            return [(0.0, 5.0, "Jetzt mit allen Wörtern.")], "de"
    engine = Better()
    again = cb.run(base, engine, backup_folders=[str(backup.parent)], log=lambda *_: None, again=True)
    assert len(engine.calls) == 2 and again["transcripts"] == first["transcripts"]
    files = os.listdir(os.path.dirname(first["transcripts"][0]))
    assert len(files) == 1                                    # replaced, no "… (2).txt"
    assert "Jetzt mit allen Wörtern." in open(first["transcripts"][0], encoding="utf-8").read()
    # and a normal run afterwards does nothing
    assert cb.run(base, FakeEngine(), backup_folders=[str(backup.parent)], log=lambda *_: None)["zip"] is None


def test_results_folder_keeps_only_recent_run_files(tmp_path):
    for day in range(1, 6):
        (tmp_path / f"Overview-2026-10-0{day}-1200.txt").write_text("x")
        (tmp_path / f"Transkripte-2026-10-0{day}-1200.zip").write_text("x")
    (tmp_path / "Model-Comparison-2026-10-22-1651.txt").write_text("x")
    cb._prune(str(tmp_path), "Overview-")
    cb._prune(str(tmp_path), "Transkripte-")
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["Model-Comparison-2026-10-22-1651.txt",
                    "Overview-2026-10-03-1200.txt", "Overview-2026-10-04-1200.txt", "Overview-2026-10-05-1200.txt",
                    "Transkripte-2026-10-03-1200.zip", "Transkripte-2026-10-04-1200.zip",
                    "Transkripte-2026-10-05-1200.zip"]


def test_text_gaps_are_named_in_the_overview():
    starts = [m * 30.0 for m in range(38)] + [1920.0, 1950.0]       # text until 18:30, again from 32:00
    assert cb.text_gaps(starts, 61 * 60) == [[1110, 1920], [1950, 3660]]
    r = {"group": "g", "source": "Replikation › 7495", "how": "timetable",
         "meta": {"course": "M13", "started": "2026-10-09T11:30:00", "gaps": [[1110, 1920]]}}
    text = cb.overview([r], [], {"g": "Transkripte/M13/x.txt"})
    assert "⚠ no text from 18:30 to 32:00 of this recording (14 min)" in text
