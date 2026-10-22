"""
Colab Batch — transcribe lecture recordings in bulk and sort them by course.

Runs in Google Colab (colab/Lecture_Transcriber.ipynb) but has no Colab
dependencies, so it is tested locally with a fake engine.

Where recordings come from
  • GoodNotes automatic backup (Drive/Goodnotes/…/*.goodnotes, read-only):
    every recording inside every notebook. Notebooks grow all semester, so
    each recording is remembered by the fingerprint the zip already stores
    (CRC + size) and transcribed only once.
  • <base>/Inbox: manual uploads — .goodnotes exports, audio files, or the
    app's “Send to Colab” zip. Moved to Inbox/Done when finished.

How each recording is sorted
  time   ← app info / audio metadata / GoodNotes file date / date in the name
  course ← the timetable event it overlaps most; a module code in the
           GoodNotes notebook name (“M13 …”) narrows or decides it
  Recordings of the same event (paused and restarted) become one transcript.

What comes out
  <base>/Transkripte/<course>/<date> – <title>.txt   readable transcripts
  <base>/Results/Transkripte-<date>.zip              same + app-import.json
                                                     (“Import from Colab…”)
  optionally a Notion page per lecture (notion_export.py)
"""

import hashlib
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
    source: str                      # where it came from (for the overview)
    key: str                         # fingerprint: same recording → same key
    open: object                     # () → path of a local audio file (extracts lazily)
    label: str                       # human name for unsorted titles
    doc: str = ""                    # GoodNotes notebook name (course hint)
    meta: dict | None = None         # lecture.json from the app, if any
    time: datetime | None = None     # local, naive
    time_is_end: bool = False        # GoodNotes file dates mark the end of a recording
    time_source: str = ""
    duration_s: float = 0.0
    inbox_file: str | None = None    # move to Inbox/Done once done


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


# ─── Finding recordings ──────────────────────────────────────

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


def _file_key(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
    return f"f:{h.hexdigest()[:16]}-{os.path.getsize(path)}"


def _extractor(zip_path: str, member: str, work: str, ext: str):
    def open_():
        dest = os.path.join(work, hashlib.sha1(f"{zip_path}|{member}".encode()).hexdigest()[:12] + ext)
        if not os.path.exists(dest):
            with zipfile.ZipFile(zip_path) as z, z.open(member) as src, open(dest, "wb") as f:
                shutil.copyfileobj(src, f)
        return dest
    return open_


def from_goodnotes(path: str, work: str, skip=frozenset()) -> list[Recording]:
    """Every recording inside a GoodNotes document (zip). Skips keys in `skip` unread."""
    doc = os.path.splitext(os.path.basename(path))[0]
    out = []
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        # A member's date is when GoodNotes wrote it — unless the export stamped
        # every file with the same time; then it's only the export date.
        dated = len({i.date_time for i in infos}) > 1
        for i in infos:
            key = f"z:{i.CRC:08x}-{i.file_size}"
            if key in skip or i.file_size < 50_000:         # known, or too small to be a lecture
                continue
            with z.open(i) as f:
                kind = _audio_kind(f.read(12))
            if not kind:
                continue
            rec = Recording(source=f"{os.path.basename(path)} › {os.path.basename(i.filename)}",
                            key=key, open=_extractor(path, i.filename, work, kind),
                            label=f"{doc} {len(out) + 1}", doc=doc)
            if dated:
                rec.time, rec.time_is_end, rec.time_source = datetime(*i.date_time), True, "GoodNotes file date"
            out.append(rec)
    return out


def from_app_zip(path: str, work: str, skip=frozenset()) -> list[Recording]:
    """The app's “Send to Colab” zip: lecture.json + audio per lecture."""
    out = []
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        for meta_info in (i for i in infos if os.path.basename(i.filename) == store.META_FILE):
            folder = os.path.dirname(meta_info.filename)
            audio = next((i for i in infos if os.path.dirname(i.filename) == folder
                          and os.path.splitext(i.filename)[1].lower() in AUDIO_EXT), None)
            if audio is None:
                continue
            key = f"z:{audio.CRC:08x}-{audio.file_size}"
            if key in skip:
                continue
            meta = json.loads(z.read(meta_info).decode("utf-8"))
            meta["_folder"] = os.path.basename(folder)
            out.append(Recording(source=f"{os.path.basename(path)} › {folder}", key=key,
                                 open=_extractor(path, audio.filename, work,
                                                 os.path.splitext(audio.filename)[1]),
                                 label=meta.get("title", "Lecture"), meta=meta))
    return out


def collect(path: str, work: str, skip=frozenset()) -> list[Recording]:
    """The not-yet-transcribed recordings in one file."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext in (".goodnotes", ".zip"):
        with zipfile.ZipFile(path) as z:
            is_app = any(os.path.basename(n) == store.META_FILE for n in z.namelist())
        return from_app_zip(path, work, skip) if is_app else from_goodnotes(path, work, skip)
    key = _file_key(path)
    if key in skip:
        return []
    return [Recording(source=name, key=key, open=lambda: path, label=os.path.splitext(name)[0])]


def inbox_files(inbox: str) -> list[str]:
    return [os.path.join(inbox, n) for n in sorted(os.listdir(inbox))
            if os.path.isfile(os.path.join(inbox, n)) and not n.startswith(".")
            and os.path.splitext(n)[1].lower() in AUDIO_EXT | {".zip", ".goodnotes"}]


def backup_files(folders: list[str]) -> list[str]:
    """All .goodnotes files in the GoodNotes backup folder(s), recursively."""
    out = []
    for folder in folders:
        for root, _, files in os.walk(folder):
            out += [os.path.join(root, f) for f in sorted(files) if f.lower().endswith(".goodnotes")]
    return out


def find_goodnotes_backup(drive_root: str) -> list[str]:
    """GoodNotes' auto-backup folder(s) in My Drive (“Goodnotes”, “GoodNotes 5”, “Apps/Goodnotes”, …)."""
    found = []
    try:
        top = sorted(os.listdir(drive_root))
    except OSError:
        return []
    for n in top:
        path = os.path.join(drive_root, n)
        if not os.path.isdir(path):
            continue
        if "goodnotes" in n.lower():
            found.append(path)
        elif n.lower() in ("apps", "apps-daten", "app data"):
            found += [os.path.join(path, m) for m in sorted(os.listdir(path))
                      if "goodnotes" in m.lower() and os.path.isdir(os.path.join(path, m))]
    return found


# ─── Matching ────────────────────────────────────────────────

def load_events(base: str):
    events = []
    for name in os.listdir(base):
        if name.lower().endswith(".ics"):
            with open(os.path.join(base, name), "r", encoding="utf-8-sig", errors="replace") as f:
                events += course_calendar.parse_ics(f.read())
    return sorted(events, key=lambda e: e.start)


_CODE = re.compile(r"(?<![A-Za-z])M\s?(\d{1,2})(?!\d)", re.I)


def course_hint(name: str, events) -> str | None:
    """'M13 Notizen' → the timetable course 'M13 I Multivariate …' (module code in the name)."""
    m = _CODE.search(name or "")
    if not m:
        return None
    code = f"M{int(m.group(1))}"
    for e in events:
        c = _CODE.match(e.course or "")
        if c and f"M{int(c.group(1))}" == code:
            return e.course
    return None


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


def plan(rec: Recording, events) -> dict:
    """Course, title, start time and group (same event → one transcript)."""
    if rec.meta:                                       # recorded with the app: already sorted
        m = dict(rec.meta, origin="app")
        folder = m.pop("_folder")
        return {"meta": m, "folder": folder, "how": "app recording", "group": f"app:{rec.key}"}

    when = rec.time
    start = (when - timedelta(seconds=rec.duration_s)) if (when and rec.time_is_end) else when
    hint = course_hint(rec.doc or rec.label, events)
    candidates = [e for e in events if e.course == hint] if hint else events
    event = match(candidates, when, rec.duration_s, rec.time_is_end) if when else None
    meta = {"language": "auto", "bookmarks": [], "origin": "upload"}
    if event:
        start = max(start, event.start - timedelta(minutes=30))
        meta.update(course=event.course, title=f"{event.kind or 'Lecture'} {event.start:%d.%m.}",
                    kind=event.kind, lecturer=event.lecturer, room=event.room,
                    calendar=event.to_meta())
        how = f"timetable ({rec.time_source})" + (" + notebook name" if hint else "")
        group = f"{event.course}|{event.start:%Y-%m-%dT%H:%M}"
    elif hint:
        meta.update(course=hint, title=f"Recording {start:%d.%m.}" if start else rec.label)
        how = "notebook name" + (f", no event at {start:%d.%m. %H:%M}" if start else ", no recording time")
        group = f"{hint}|{start:%Y-%m-%d}" if start else f"rec:{rec.key}"
    else:
        meta.update(course=UNSORTED, title=rec.label)
        how = (f"no timetable event at {start:%d.%m.%Y %H:%M} ({rec.time_source})" if start
               else "no recording time found")
        group = f"rec:{rec.key}"
    meta["started"] = start.strftime("%Y-%m-%dT%H:%M:%S") if start else ""
    stamp = start.strftime("%Y-%m-%d %H-%M") if start else "undated"
    return {"meta": meta, "folder": f"{stamp} – {store.safe_name(meta['title'], 'Recording')}",
            "how": how, "group": group}


# ─── Transcription ───────────────────────────────────────────

def load_audio(path: str) -> np.ndarray:
    from faster_whisper import decode_audio
    return decode_audio(path, sampling_rate=SR)


def transcribe(rec: Recording, events, engine, language: str | None = "de") -> dict:
    """Transcribe one recording → a result entry (kept in state until written out)."""
    from transcriber import clean_segment              # repetition loops, silence fillers
    p = plan(rec, events)
    meta = p["meta"]
    audio = load_audio(rec.open())
    rec.duration_s = len(audio) / SR

    lang = meta.get("language") if meta.get("language") not in (None, "", "auto") else language
    # No course-name prompt here: tested on a real seminar, large models repeat the prompt
    # in noisy stretches ("M13 I. M13 I. …") and lose real speech around it.
    t0 = time.time()
    raw, detected = engine.transcribe(audio, lang, None)
    segments = [(s, e, t) for s, e, t in ((s, e, clean_segment(t)) for s, e, t in raw) if t]

    meta.update(duration_s=round(rec.duration_s, 1), detected_languages=[detected] if detected else [],
                model=engine.name, transcribed_by="colab", status=store.STATUS_COMPLETE,
                transcribed_until_s=round(rec.duration_s, 1), failed_chunks=0)
    session = tempfile.mkdtemp(prefix="session-")      # the app's writer does paragraphs
    writer = store.TranscriptWriter(session)
    writer.write_header(meta)
    for b in meta.get("bookmarks", []):
        writer.add_bookmark(b["t"], b.get("label", "Bookmark"))
    writer.append_segments(segments)
    writer.finish()
    with open(os.path.join(session, store.TRANSCRIPT_MD), encoding="utf-8") as f:
        md = f.read()
    shutil.rmtree(session, ignore_errors=True)
    return {"key": rec.key, "source": rec.source, "group": p["group"], "folder": p["folder"],
            "how": p["how"], "meta": meta, "transcript_md": md,
            "minutes": round(rec.duration_s / 60), "seconds_taken": round(time.time() - t0)}


# ─── Writing transcripts ─────────────────────────────────────

_TS_LINE = re.compile(r"^(> 🔖 |> ⚠ )?\*\*\[(\d\d):(\d\d):(\d\d)\]\*\*\s?(.*)$")


def _body(transcript_md: str) -> list[str]:
    """Transcript paragraphs as '[mm:ss]  text' lines."""
    lines = transcript_md.splitlines()
    out = []
    for line in lines[store._strip_front_matter(lines):]:
        if not line.strip() or store.is_header_line(line):
            continue
        m = _TS_LINE.match(line)
        if not m:
            out.append(line)
            continue
        prefix, h, mi, s, text = m.groups()
        ts = f"{int(h)}:{mi}:{s}" if int(h) else f"{mi}:{s}"
        mark = "★ " if prefix and "🔖" in prefix else ("⚠ " if prefix else "")
        out.append(f"{mark}[{ts}]  {text}")
    return out


def lecture_text(parts: list[dict]) -> str:
    """Readable text of one lecture (one or more recordings of the same event)."""
    meta = parts[0]["meta"]
    info = []
    try:
        info.append(f"{datetime.fromisoformat(meta['started']):%d.%m.%Y, %H:%M}")
    except (KeyError, ValueError):
        pass
    info.append(f"{sum(p['meta'].get('duration_s', 0) for p in parts) / 60:.0f} min")
    info += [meta[k] for k in ("lecturer", "room") if meta.get(k)]
    out = [meta.get("title", ""), meta.get("course", ""), " · ".join(info), ""]
    for n, p in enumerate(parts, 1):
        if len(parts) > 1:
            try:
                at = f" ({datetime.fromisoformat(p['meta']['started']):%H:%M})"
            except (KeyError, ValueError):
                at = ""
            out += [f"— Part {n}{at} —", ""]
        for line in _body(p["transcript_md"]):
            out += [line, ""]
    return "\n".join(out).rstrip() + "\n"


def lectures(results: list[dict]) -> list[list[dict]]:
    """Group results by event, parts in time order."""
    groups: dict[str, list[dict]] = {}
    for r in results:
        groups.setdefault(r["group"], []).append(r)
    out = [sorted(g, key=lambda r: r["meta"].get("started", "")) for g in groups.values()]
    return sorted(out, key=lambda g: (g[0]["meta"]["course"], g[0]["meta"].get("started", "")))


def _unique(path: str, ext: str) -> str:
    candidate, n = path + ext, 2
    while os.path.exists(candidate):
        candidate, n = f"{path} ({n}){ext}", n + 1
    return candidate


def write_transcripts(groups: list[list[dict]], out_dir: str) -> list[str]:
    """<out_dir>/<course>/<date> – <title>.txt for each lecture. Returns the paths."""
    paths = []
    for parts in groups:
        meta = parts[0]["meta"]
        course_dir = os.path.join(out_dir, store.safe_name(meta["course"]))
        os.makedirs(course_dir, exist_ok=True)
        date = meta.get("started", "")[:10] or "undated"
        path = _unique(os.path.join(course_dir, f"{date} – {store.safe_name(meta['title'], 'Recording')}"),
                       ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(lecture_text(parts))
        paths.append(path)
    return paths


def overview(results: list[dict], errors: list[dict], files: dict[str, str]) -> str:
    """Which recording became which transcript, and what needs a look."""
    lines = [f"Transcribed {datetime.now():%d.%m.%Y %H:%M}", ""]
    for r in sorted(results, key=lambda r: (r["meta"]["course"], r["meta"].get("started", ""))):
        mark = "?" if r["meta"]["course"] == UNSORTED else "✓"
        lines.append(f"{mark} {files.get(r['group'], '')}   ← {r['source']} ({r['how']})")
    for e in errors:
        lines.append(f"✗ {e['source']}: {e['error']}")
    if any(r["meta"]["course"] == UNSORTED for r in results):
        lines += ["", "“?” = no course found (no recording time, nothing scheduled then, and no",
                  "module code like “M13” in the GoodNotes notebook name)."]
    return "\n".join(lines) + "\n"


# ─── Run ─────────────────────────────────────────────────────

def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def run(base: str, engine, language: str | None = "de", backup_folders=(), publish=None,
        log=print) -> dict:
    """
    Transcribe everything new in <base>/Inbox and the GoodNotes backup folder(s).

    publish(groups) is called with the finished lectures (e.g. Notion upload).
    Returns {"transcripts": [paths], "zip": path or None, "unsorted": n, "failed": n}.
    """
    inbox, results_dir = os.path.join(base, "Inbox"), os.path.join(base, "Results")
    done, state_dir = os.path.join(inbox, "Done"), os.path.join(base, ".state")
    out_dir = os.path.join(base, "Transkripte")
    for d in (inbox, done, results_dir, state_dir):
        os.makedirs(d, exist_ok=True)
    processed_path = os.path.join(state_dir, "processed.json")    # key → transcript file
    pending_path = os.path.join(state_dir, "pending.json")        # transcribed, not yet written
    processed = _load(processed_path, {})
    pending = _load(pending_path, [])

    events = load_events(base)
    log(f"Timetable: {len(events)} events" if events else
        "⚠ No timetable (.ics) in the folder — sorting only by GoodNotes notebook names.")

    skip = set(processed) | {r["key"] for r in pending}
    sources = [(p, False) for p in backup_files(list(backup_folders))] + \
              [(p, True) for p in inbox_files(inbox)]
    work = tempfile.mkdtemp(prefix="lectures-")
    todo: list[Recording] = []
    errors = []
    for path, is_inbox in sources:
        try:
            recs = collect(path, work, skip)
        except Exception as e:
            errors.append({"source": os.path.basename(path), "error": f"can't open: {e}"})
            continue
        for r in recs:
            r.inbox_file = path if is_inbox else None
            skip.add(r.key)                       # the same recording in two files → once
        todo += recs
        if is_inbox and not recs:
            shutil.move(path, os.path.join(done, os.path.basename(path)))
    log(f"{len(todo)} new recording(s) to transcribe"
        + (f" ({len(pending)} already done before a disconnect)" if pending else "") + ".")

    failed_files = set()
    for n, rec in enumerate(todo, 1):
        try:
            audio_path = rec.open()
            rec.duration_s, meta_time = probe(audio_path)
            if rec.meta is None:
                if meta_time:
                    rec.time, rec.time_is_end, rec.time_source = meta_time, False, "audio metadata"
                elif rec.time is None and time_from_name(rec.label):
                    rec.time, rec.time_source = time_from_name(rec.label), "file name"
            if rec.duration_s and rec.duration_s < MIN_DURATION_S:
                log(f"[{n}/{len(todo)}] skipped (only {rec.duration_s:.0f} s): {rec.source}")
                processed[rec.key] = {"skipped": "too short"}
            else:
                log(f"[{n}/{len(todo)}] {rec.source} ({rec.duration_s / 60:.0f} min)…")
                r = transcribe(rec, events, engine, language)
                pending.append(r)
                log(f"      → {r['meta']['course']} / {r['meta']['title']}   "
                    f"[{r['how']}, {r['seconds_taken']} s]")
            _save(pending_path, pending)
            _save(processed_path, processed)
        except Exception as e:
            errors.append({"source": rec.source, "error": str(e)})
            failed_files.add(rec.inbox_file)
            log(f"      ✗ failed: {e}")
        # Inbox file finished (and nothing failed) → Done. Not on a disconnect:
        # then the file stays and a rerun picks up its unfinished recordings.
        f = rec.inbox_file
        if f and f not in failed_files and os.path.exists(f) and \
                not any(r.inbox_file == f for r in todo[n:]):
            shutil.move(f, os.path.join(done, os.path.basename(f)))
    shutil.rmtree(work, ignore_errors=True)

    if not pending:
        for e in errors:
            log(f"✗ {e['source']}: {e['error']}")
        log("Nothing new to transcribe." if not errors else "Nothing transcribed.")
        return {"transcripts": [], "zip": None, "unsorted": 0, "failed": len(errors)}

    groups = lectures(pending)
    paths = write_transcripts(groups, out_dir)
    files = {g[0]["group"]: os.path.relpath(p, base).replace(os.sep, "/") for g, p in zip(groups, paths)}
    if publish:
        try:
            publish(groups)
        except Exception as e:
            log(f"⚠ Publishing failed (transcripts are still in Drive): {e}")

    zip_path = os.path.join(results_dir, f"Transkripte-{datetime.now():%Y-%m-%d-%H%M}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, "Transcripts/" + os.path.relpath(p, out_dir).replace(os.sep, "/"))
        z.writestr(APP_IMPORT, json.dumps(
            [{"folder": r["folder"], "meta": r["meta"], "transcript_md": r["transcript_md"]}
             for r in pending], ensure_ascii=False))
    text = overview(pending, errors, files)
    with open(os.path.join(results_dir, f"Overview-{datetime.now():%Y-%m-%d-%H%M}.txt"), "w",
              encoding="utf-8") as f:
        f.write(text)

    for r in pending:
        processed[r["key"]] = {"file": files[r["group"]], "source": r["source"]}
    _save(processed_path, processed)
    os.remove(pending_path)

    unsorted = sum(1 for r in pending if r["meta"]["course"] == UNSORTED)
    log("\n" + text)
    log(f"Done: {len(groups)} transcript(s) in {os.path.basename(base)}/Transkripte, "
        f"{unsorted} unsorted, {len(errors)} failed.")
    return {"transcripts": paths, "zip": zip_path, "unsorted": unsorted, "failed": len(errors)}


# ─── Engine (Colab GPU) ──────────────────────────────────────

MODELS = {
    # name in the notebook → Hugging Face repo (CTranslate2 format).
    # Compared on a real noisy seminar (Oct 2026): large-v3 caught the distant lecturer
    # best; turbo is ~2× faster and a little less accurate. The German fine-tunes on
    # the Hub (CT2 conversions of primeline/*) produced only garbage, so they're left out.
    "large-v3": "large-v3",
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
}

# Speech detection: 0.5 is Silero's default; 0.35 also keeps a lecturer speaking from the
# front of the room, which the default dropped in the test recording.
VAD_THRESHOLD = 0.35


class WhisperEngine:
    """faster-whisper on the GPU, batched (a 90-min lecture takes a few minutes on a T4)."""

    def __init__(self, model: str = "large-v3", device: str = "cuda", batch_size: int = 16,
                 vad_threshold: float | None = VAD_THRESHOLD):
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        compute = "float16" if device == "cuda" else "int8"
        self.name = model
        self.batch_size = batch_size if device == "cuda" else 4
        self.vad = {"threshold": vad_threshold} if vad_threshold else None
        self._model = BatchedInferencePipeline(
            WhisperModel(MODELS.get(model, model), device=device, compute_type=compute))

    def transcribe(self, audio, language: str | None, prompt: str | None):
        kwargs = dict(language=language, batch_size=self.batch_size, beam_size=5)
        if self.vad:
            kwargs["vad_parameters"] = self.vad
        try:
            segments, info = self._model.transcribe(audio, initial_prompt=prompt, **kwargs)
        except TypeError:                     # older faster-whisper without prompt support
            segments, info = self._model.transcribe(audio, **kwargs)
        return [(s.start, s.end, s.text) for s in segments], info.language
