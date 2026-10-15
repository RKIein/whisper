"""
Colab Batch — transcribe a pile of lecture recordings and sort them by course.

Runs in Google Colab (see colab/Lecture_Transcriber.ipynb), but has no Colab
dependencies, so it can be tested locally with a fake engine.

  <base>/Inbox/      recordings to process:
                       • zips from the app's “Send to Colab…” (lecture.json + audio)
                       • GoodNotes exports (.goodnotes) — every recording inside
                       • loose audio files (.m4a, .mp3, .wav, …)
  <base>/*.ics       your timetable (TraiNex export)
  <base>/Results/    Transkripte-<date>.zip:
                       Transcripts/<course>/<date> – <title>.txt   ← the transcripts
                       Overview.txt                               ← what went where
                       app-import.json                            ← for “Import from Colab…”

Each recording gets a time (app info → audio metadata → GoodNotes file date →
file name), is matched to the timetable event it overlaps most, and
transcribed. Recordings that match nothing go to the course “Unsorted”.
"""

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

import course_calendar
import lecture_store as store

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".caf", ".ogg", ".opus", ".flac",
             ".webm", ".mp4", ".mov", ".3gp", ".amr", ".wma"}
UNSORTED = "Unsorted"
APP_IMPORT = "app-import.json"
SR = 16000
MIN_DURATION_S = 30          # skip accidental taps
MIN_OVERLAP_S = 10 * 60      # how much a recording must overlap an event to count
LOCAL_TZ = "Europe/Berlin"


@dataclass
class Recording:
    source: str                      # input file (for the report)
    audio: str                       # extracted audio file
    label: str                       # human name for unsorted titles
    meta: dict | None = None         # lecture.json from the app, if any
    time: datetime | None = None     # local, naive
    time_is_end: bool = False        # GoodNotes file dates mark the end of the recording
    time_source: str = ""
    duration_s: float = 0.0
    result: dict = field(default_factory=dict)


# ─── Recording time ──────────────────────────────────────────

def _to_local(dt: datetime) -> datetime:
    """Aware datetime → local naive (the timetable is local time)."""
    if dt.tzinfo is None:
        return dt
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(LOCAL_TZ)).replace(tzinfo=None)
    except Exception:
        return dt.astimezone().replace(tzinfo=None)


def _parse_tag_time(value: str) -> datetime | None:
    """'2026-10-06T11:52:00.000000Z' or '2026-10-06T13:52:00+0200' → local naive."""
    v = value.strip().replace("Z", "+00:00")
    v = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", v)            # +0200 → +02:00
    v = re.sub(r"\.\d+", "", v)                               # drop fractions
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.year < 2000:                                        # 1904/1970 = unset
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)                  # MP4 creation_time is UTC
    return _to_local(dt)


# "2026-10-06 13-52", "20261006_135200", "2026-10-06 at 13.52", "06.10.2026 um 13:52"
_NAME_TIMES = [
    (re.compile(r"(20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)\D{0,5}?(\d\d)[-_.:h]?(\d\d)"), (0, 1, 2, 3, 4)),
    (re.compile(r"(\d\d)\.(\d\d)\.(20\d\d)\D{0,5}?(\d\d)[.:h](\d\d)"), (2, 1, 0, 3, 4)),
]


def time_from_name(name: str) -> datetime | None:
    for pattern, order in _NAME_TIMES:
        m = pattern.search(name)
        if m:
            g = m.groups()
            try:
                return datetime(*(int(g[i]) for i in order))
            except ValueError:
                continue
    return None


def probe(path: str) -> tuple[float, datetime | None]:
    """Duration (s) and recording time from the file's metadata, via PyAV."""
    import av
    with av.open(path) as c:
        duration = (c.duration or 0) / 1_000_000
        tags = dict(c.metadata or {})
        for s in c.streams:
            tags.update({k: v for k, v in (s.metadata or {}).items() if k not in tags})
        if not duration and c.streams.audio:
            s = c.streams.audio[0]
            duration = float(s.duration * s.time_base) if s.duration else 0.0
    for key in ("com.apple.quicktime.creationdate", "creation_time", "date"):
        if key in tags:
            t = _parse_tag_time(str(tags[key]))
            if t:
                return duration, t
    return duration, None


# ─── Collecting inputs ───────────────────────────────────────

def _audio_kind(head: bytes) -> str | None:
    """Recognise audio by its first bytes (GoodNotes stores files without extensions)."""
    if head[4:8] == b"ftyp":
        return ".m4a"
    if head[:4] == b"caff":
        return ".caf"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if head[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return ".aac"
    return None


def inbox_files(inbox: str) -> list[str]:
    files = []
    for name in sorted(os.listdir(inbox)):
        path = os.path.join(inbox, name)
        ext = os.path.splitext(name)[1].lower()
        if os.path.isfile(path) and not name.startswith(".") and \
                (ext in (".zip", ".goodnotes") or ext in AUDIO_EXT):
            files.append(path)
    return files


def collect(path: str, work: str) -> list[Recording]:
    """The recording(s) in one inbox file."""
    name = os.path.basename(path)
    if os.path.splitext(name)[1].lower() in (".zip", ".goodnotes"):
        return _from_archive(path, work)
    return [Recording(source=name, audio=path, label=os.path.splitext(name)[0])]


def _from_archive(path: str, work: str) -> list[Recording]:
    """App export zip (lecture.json per session) or GoodNotes document (embedded audio)."""
    name = os.path.basename(path)
    out = []
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        sessions = [i for i in infos if os.path.basename(i.filename) == store.META_FILE]
        if sessions:
            for meta_info in sessions:
                folder = os.path.dirname(meta_info.filename)
                meta = json.loads(z.read(meta_info).decode("utf-8"))
                audio_info = next((i for i in infos if os.path.dirname(i.filename) == folder
                                   and os.path.splitext(i.filename)[1].lower() in AUDIO_EXT), None)
                if audio_info is None:
                    continue
                dest = os.path.join(work, f"app-{len(os.listdir(work))}"
                                          f"{os.path.splitext(audio_info.filename)[1]}")
                with z.open(audio_info) as src, open(dest, "wb") as f:
                    shutil.copyfileobj(src, f)
                rec = Recording(source=f"{name} › {folder}", audio=dest,
                                label=meta.get("title", "Lecture"), meta=meta)
                rec.meta["_folder"] = os.path.basename(folder)
                out.append(rec)
            return out

        # GoodNotes: any member that is audio
        doc = os.path.splitext(name)[0]
        stamps = {i.date_time for i in infos}
        for i in infos:
            with z.open(i) as f:
                kind = _audio_kind(f.read(12))
            if not kind:
                continue
            dest = os.path.join(work, f"gn-{len(os.listdir(work))}{kind}")
            with z.open(i) as src, open(dest, "wb") as f:
                shutil.copyfileobj(src, f)
            rec = Recording(source=f"{name} › {os.path.basename(i.filename)}", audio=dest,
                            label=f"{doc} {len(out) + 1}")
            # A member's date is when GoodNotes wrote it — unless the export stamped
            # every file with the same time, then it's only the export date.
            if len(stamps) > 1 or len(infos) == 1:
                rec.time = datetime(*i.date_time)
                rec.time_is_end = True
                rec.time_source = "GoodNotes file date"
            out.append(rec)
    return out


# ─── Matching ────────────────────────────────────────────────

def match(events, start: datetime, duration_s: float, time_is_end: bool = False):
    """The timetable event this recording overlaps most (or None)."""
    windows = [(start - timedelta(seconds=duration_s), start)] if time_is_end else []
    windows.append((start, start + timedelta(seconds=duration_s)))
    need = min(MIN_OVERLAP_S, 0.3 * duration_s)
    best, best_overlap = None, 0.0
    for e in events:
        if e.end - e.start >= timedelta(hours=12):
            continue
        for a, b in windows:
            overlap = (min(b, e.end) - max(a, e.start)).total_seconds()
            if overlap > best_overlap:
                best, best_overlap = e, overlap
    return best if best_overlap >= need else None


def load_events(base: str):
    events = []
    for name in os.listdir(base):
        if name.lower().endswith(".ics"):
            with open(os.path.join(base, name), "r", encoding="utf-8-sig", errors="replace") as f:
                events += course_calendar.parse_ics(f.read())
    return sorted(events, key=lambda e: e.start)


# ─── Audio ───────────────────────────────────────────────────

def load_audio(path: str) -> np.ndarray:
    from faster_whisper import decode_audio
    return decode_audio(path, sampling_rate=SR)


# ─── Processing ──────────────────────────────────────────────

def plan(rec: Recording, events) -> dict:
    """Decide course, title and folder for a recording (no transcription yet)."""
    if rec.meta:                                    # recorded with the app: already sorted
        m = dict(rec.meta, origin="app")
        folder = m.pop("_folder")
        return {"meta": m, "folder": folder, "how": "app recording"}

    when = rec.time
    meta = {"started": "", "language": "auto", "bookmarks": [], "origin": "upload"}
    event = match(events, when, rec.duration_s, rec.time_is_end) if when else None
    if event:
        start = when - timedelta(seconds=rec.duration_s) if rec.time_is_end else when
        start = max(start, event.start - timedelta(minutes=30))
        title = f"{event.kind or 'Lecture'} {event.start:%d.%m.}"
        meta.update(course=event.course, title=title, kind=event.kind, lecturer=event.lecturer,
                    room=event.room, calendar=event.to_meta(),
                    started=start.strftime("%Y-%m-%dT%H:%M:%S"))
        how = f"timetable ({rec.time_source})"
    else:
        start = when - timedelta(seconds=rec.duration_s) if (when and rec.time_is_end) else when
        meta.update(course=UNSORTED, title=rec.label,
                    started=start.strftime("%Y-%m-%dT%H:%M:%S") if start else "")
        how = (f"no timetable event at {start:%d.%m.%Y %H:%M} ({rec.time_source})" if start
               else "no recording time found")
    stamp = start.strftime("%Y-%m-%d %H-%M") if start else "undated"
    folder = f"{stamp} – {store.safe_name(meta['title'], 'Recording')}"
    return {"meta": meta, "folder": folder, "how": how}


_TS_LINE = re.compile(r"^(> 🔖 |> ⚠ )?\*\*\[(\d\d):(\d\d):(\d\d)\]\*\*\s?(.*)$")


def readable(meta: dict, transcript_md: str) -> str:
    """The app's transcript.md → plain text for reading: header, then [mm:ss] paragraphs."""
    lines = transcript_md.splitlines()
    body = lines[store._strip_front_matter(lines):]
    info = []
    try:
        info.append(f"{datetime.fromisoformat(meta['started']):%d.%m.%Y, %H:%M}")
    except (KeyError, ValueError):
        pass
    info.append(f"{round(meta.get('duration_s', 0) / 60)} min")
    info += [meta[k] for k in ("lecturer", "room") if meta.get(k)]
    out = [meta.get("title", ""), meta.get("course", ""), " · ".join(info), ""]
    for line in body:
        if not line.strip() or store.is_header_line(line):
            continue
        m = _TS_LINE.match(line)
        if not m:
            out += [line, ""]
            continue
        prefix, h, mi, s, text = m.groups()
        ts = f"{int(h)}:{mi}:{s}" if int(h) else f"{mi}:{s}"
        mark = "★ " if prefix and "🔖" in prefix else ("⚠ " if prefix else "")
        out += [f"{mark}[{ts}]  {text}", ""]
    return "\n".join(out).rstrip() + "\n"


def _unique(path: str, ext: str) -> str:
    candidate, n = path + ext, 2
    while os.path.exists(candidate):
        candidate, n = f"{path} ({n}){ext}", n + 1
    return candidate


def process(rec: Recording, events, engine, out_dir: str, language: str = "de") -> dict:
    """Transcribe one recording → readable text file + an entry for the app import."""
    p = plan(rec, events)
    meta, how = p["meta"], p["how"]
    session = tempfile.mkdtemp(prefix="session-")      # scratch for the transcript writer

    audio = load_audio(rec.audio)
    rec.duration_s = len(audio) / SR

    lang = meta.get("language") if meta.get("language") not in (None, "", "auto") else language
    prompt = None
    if meta["course"] != UNSORTED:
        prompt = f"{meta['kind']}: {meta['course']}." if meta.get("kind") else f"{meta['course']}."
    t0 = time.time()
    raw, detected = engine.transcribe(audio, lang, prompt)
    from transcriber import clean_segment       # repetition loops, silence fillers
    segments = [(s, e, t) for s, e, t in ((s, e, clean_segment(t)) for s, e, t in raw) if t]

    meta.update(duration_s=round(rec.duration_s, 1), detected_languages=[detected] if detected else [],
                model=engine.name, transcribed_by="colab", status=store.STATUS_COMPLETE,
                transcribed_until_s=round(rec.duration_s, 1), failed_chunks=0)
    writer = store.TranscriptWriter(session)
    writer.write_header(meta)
    for b in meta.get("bookmarks", []):
        writer.add_bookmark(b["t"], b.get("label", "Bookmark"))
    writer.append_segments(segments)
    writer.finish()
    with open(os.path.join(session, store.TRANSCRIPT_MD), encoding="utf-8") as f:
        md = f.read()
    shutil.rmtree(session, ignore_errors=True)

    # What you read: Transcripts/<course>/<date> – <title>.txt
    course_dir = os.path.join(out_dir, "Transcripts", store.safe_name(meta["course"]))
    os.makedirs(course_dir, exist_ok=True)
    date = meta.get("started", "")[:10] or "undated"
    txt = _unique(os.path.join(course_dir, f"{date} – {store.safe_name(meta['title'], 'Recording')}"), ".txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write(readable(meta, md))

    return {"course": meta["course"], "title": meta["title"], "folder": p["folder"],
            "started": meta.get("started", ""), "how": how, "file": os.path.relpath(txt, out_dir).replace(os.sep, "/"),
            "minutes": round(rec.duration_s / 60), "seconds_taken": round(time.time() - t0),
            "app": {"folder": p["folder"], "meta": meta, "transcript_md": md}}


def overview(report: list[dict]) -> str:
    """Overview.txt: which recording became which transcript, and what needs a look."""
    lines = [f"Transcribed {datetime.now():%d.%m.%Y %H:%M}", ""]
    for r in sorted(report, key=lambda r: (r.get("course", "~"), r.get("started", ""))):
        if "error" in r:
            lines.append(f"✗ {r['source']}: {r['error']}")
        elif r["course"] == UNSORTED:
            lines.append(f"? {r['file']}   ← {r['source']} ({r['how']})")
        else:
            lines.append(f"✓ {r['file']}   ← {r['source']}")
    unsorted = [r for r in report if r.get("course") == UNSORTED]
    if unsorted:
        lines += ["", "“?” = no timetable match (no recording time, or nothing scheduled then).",
                  "They are in Transcripts/Unsorted; in the app use More ▾ → Move to course."]
    return "\n".join(lines) + "\n"


def run(base: str, engine, language: str = "de", log=print) -> str | None:
    """Process everything in <base>/Inbox. Returns the results zip (or None)."""
    inbox = os.path.join(base, "Inbox")
    done = os.path.join(inbox, "Done")
    results = os.path.join(base, "Results")
    pending = os.path.join(results, "pending")          # survives a disconnect
    for d in (inbox, done, pending):
        os.makedirs(d, exist_ok=True)

    events = load_events(base)
    log(f"Timetable: {len(events)} events" if events else
        "⚠ No timetable (.ics) in the folder — everything will go to “Unsorted”.")

    files = inbox_files(inbox)
    log(f"{len(files)} file(s) in the Inbox.")
    report_path = os.path.join(pending, "state.json")
    report = json.load(open(report_path, encoding="utf-8")) if os.path.exists(report_path) else []
    out_root = pending

    for k, path in enumerate(files, 1):
        work = tempfile.mkdtemp(prefix="lectures-")
        try:
            recs = collect(path, work)
        except Exception as e:
            recs = []
            report.append({"source": os.path.basename(path), "error": f"can't open: {e}"})
            log(f"✗ {os.path.basename(path)}: can't open ({e})")
        for rec in recs:
            try:
                rec.duration_s, meta_time = probe(rec.audio)
                if rec.meta is None:
                    if meta_time:
                        rec.time, rec.time_is_end, rec.time_source = meta_time, False, "audio metadata"
                    elif rec.time is None and time_from_name(rec.label):
                        rec.time, rec.time_source = time_from_name(rec.label), "file name"
                if rec.duration_s and rec.duration_s < MIN_DURATION_S:
                    log(f"[{k}/{len(files)}] skipped (only {rec.duration_s:.0f} s): {rec.source}")
                    continue
                log(f"[{k}/{len(files)}] {rec.source} ({rec.duration_s / 60:.0f} min)…")
                r = process(rec, events, engine, out_root, language)
                r["source"] = rec.source
                report.append(r)
                log(f"      → {r['course']} / {r['title']}   [{r['how']}, {r['seconds_taken']} s]")
            except Exception as e:
                report.append({"source": rec.source, "error": str(e)})
                log(f"      ✗ failed: {e}")
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
        shutil.rmtree(work, ignore_errors=True)
        # Done with this file — move it, so a rerun after a disconnect skips it
        shutil.move(path, os.path.join(done, os.path.basename(path)))

    if not report:
        log("Nothing new to transcribe.")
        return None
    ok = [r for r in report if "error" not in r]
    unsorted = [r for r in ok if r["course"] == UNSORTED]
    zip_path = os.path.join(results, f"Transkripte-{datetime.now():%Y-%m-%d-%H%M}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        transcripts = os.path.join(pending, "Transcripts")
        for root, _, files in os.walk(transcripts):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, pending))
        z.writestr("Overview.txt", overview(report))
        # For “Import from Colab…” in the app (files the transcripts into your library)
        z.writestr(APP_IMPORT, json.dumps([r["app"] for r in ok], ensure_ascii=False))
    shutil.rmtree(pending)
    log(f"\nDone: {len(ok)} transcribed, {len(unsorted)} unsorted, "
        f"{len(report) - len(ok)} failed → {os.path.basename(zip_path)}")
    return zip_path


# ─── Engine (Colab GPU) ──────────────────────────────────────

class WhisperEngine:
    """faster-whisper on the GPU, batched (≈2–4 min per 90-min lecture on a T4)."""

    def __init__(self, model: str = "large-v3", device: str = "cuda", batch_size: int = 16):
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        compute = "float16" if device == "cuda" else "int8"
        self.name = model
        self.batch_size = batch_size if device == "cuda" else 4
        self._model = BatchedInferencePipeline(WhisperModel(model, device=device, compute_type=compute))

    def transcribe(self, audio, language: str | None, prompt: str | None):
        kwargs = dict(language=language, batch_size=self.batch_size, beam_size=5)
        try:
            segments, info = self._model.transcribe(audio, initial_prompt=prompt, **kwargs)
        except TypeError:                     # older faster-whisper without prompt support
            segments, info = self._model.transcribe(audio, **kwargs)
        return [(s.start, s.end, s.text) for s in segments], info.language
