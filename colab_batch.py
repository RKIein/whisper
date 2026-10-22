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
import io
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
    slides: object = None            # () → text of the notebook's slide PDF ('' if none)


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
    slides = _cached(lambda: slide_text(path))
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
                            label=f"{doc} {len(out) + 1}", doc=doc, slides=slides)
            if dated:
                rec.time, rec.time_is_end, rec.time_source = datetime(*i.date_time), True, "GoodNotes file date, end"
            out.append(rec)
    return out


def _cached(fn):
    """fn() once, on first call (a notebook's slides are shared by its recordings)."""
    box = []

    def get():
        if not box:
            box.append(fn())
        return box[0]
    return get


def slide_text(path: str) -> str:
    """Text of the slide PDF(s) imported into a GoodNotes notebook ('' if none/unreadable)."""
    try:
        import pypdf
    except ImportError:
        # An already-open Colab notebook keeps its old setup cell, which didn't install pypdf
        # (that's how the first run lost all umlaut fixes) — so fetch it here.
        import subprocess
        import sys
        if not sys.platform.startswith("linux"):
            return ""
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pypdf"], check=False)
        try:
            import pypdf
        except ImportError:
            return ""
    pages = []
    try:
        with zipfile.ZipFile(path) as z:
            for i in z.infolist():
                if i.is_dir() or i.file_size < 1000:
                    continue
                with z.open(i) as f:
                    if f.read(5) != b"%PDF-":
                        continue
                try:
                    pages += [p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(z.read(i))).pages]
                except Exception:
                    continue
    except (OSError, zipfile.BadZipFile):
        return ""
    return "\n".join(pages)


_WORD = re.compile(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß-]*[A-Za-zÄÖÜäöüß]")


def fix_umlauts(title: str, slides: str) -> str:
    """'Einfuehrung' (file names avoid umlauts) → 'Einführung' when the slides spell it so."""
    words = {w.lower(): w for w in _WORD.findall(slides)}

    def fix(m):
        w = m.group(0)
        for a, b in (("ae", "ä"), ("oe", "ö"), ("ue", "ü"), ("Ae", "Ä"), ("Oe", "Ö"), ("Ue", "Ü")):
            w = w.replace(a, b)
        if w == m.group(0):
            return w
        for candidate in (w, w.replace("ss", "ß")):
            found = words.get(candidate.lower())
            if found:             # the slides' spelling, but not a heading's 'EFFEKTGRÖßEN'
                return candidate if found[1:2].isupper() else found
        return m.group(0)
    return _WORD.sub(fix, title)


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


def backup_files(folders: list[str], only: str = "") -> list[str]:
    """All .goodnotes files in the GoodNotes backup folder(s), recursively.
    only: keep files whose path inside the backup contains this (e.g. a GoodNotes folder name)."""
    out = []
    for folder in folders:
        for root, _, files in os.walk(folder):
            for f in sorted(files):
                path = os.path.join(root, f)
                if f.lower().endswith(".goodnotes") and \
                        (not only or only.lower() in os.path.relpath(path, folder).lower()):
                    out.append(path)
    return out


def semester_start(events) -> datetime | None:
    """Two weeks before the first timetable event — older recordings are from earlier semesters."""
    return min(e.start for e in events) - timedelta(days=14) if events else None


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
    """
    The timetable course a GoodNotes notebook belongs to, from module codes:
    'M. Sc. Psychotherapie/M13 Multivariate …/3_Replikation' → 'M13 I Multivariate …'.
    The nearest folder with a code wins (that's how you sorted it), then the notebook name.
    """
    parts = [p for p in re.split(r"[\\/]", name or "") if p]
    for part in [*reversed(parts[:-1]), *parts[-1:]]:
        m = _CODE.search(part)
        if not m:
            continue
        code = f"M{int(m.group(1))}"
        for e in events:
            c = _CODE.match(e.course or "")
            if c and f"M{int(c.group(1))}" == code:
                return e.course
    return None


def match(events, start: datetime, duration_s: float, time_is_end: bool = False):
    """
    The timetable event this recording overlaps most (or None). time_is_end: the time
    marks the end (GoodNotes); that reading is tried first, the start reading as a fallback.
    """
    windows = [(start - timedelta(seconds=duration_s), start)] if time_is_end else []
    windows.append((start, start + timedelta(seconds=duration_s)))
    need = min(MIN_OVERLAP_S, 0.3 * duration_s)
    for a, b in windows:
        best, best_overlap = None, 0.0
        for e in events:
            if e.end - e.start >= timedelta(hours=12):
                continue
            overlap = (min(b, e.end) - max(a, e.start)).total_seconds()
            if overlap > best_overlap:
                best, best_overlap = e, overlap
        if best_overlap >= need:
            return best
    return None


_TOPIC_NOISE = {"termin", "notizen", "vorlesung", "seminar", "sitzung", "folien", "skript"}


def topic(doc: str, course: str = "") -> str:
    """
    A lecture topic from a GoodNotes notebook name, or '' if it has none:
    '…/3_Replikation_v17071.pdf' → 'Replikation',
    'VL_DMM_02_-_Itemanalyse_und_Exploratorische_Faktorenanalyse' → 'Itemanalyse und …',
    'M4_M6_Termin_2' / 'M1_2_IH' / a notebook named after its course → ''.
    """
    name = re.split(r"[\\/]", doc or "")[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.I)
    name = re.sub(r"(_v\d+)?( \d+)?$", "", name)          # '_v17071', ' 2' (duplicate copy)
    name = name.replace("_", " ").strip()
    if " - " in name:                                     # 'VL DMM 02 - Itemanalyse …'
        name = name.split(" - ", 1)[1]
    name = re.sub(r"^(M\d+\s+)*(\d+\s+)?", "", name).strip()  # leading 'M8 ', '3 ', 'M1 2 '
    words = re.findall(r"[A-Za-zÄÖÜäöüß]{5,}", name)
    in_course = set(re.findall(r"[a-zäöüß]{5,}", course.lower()))
    if not [w for w in words if w.lower() not in _TOPIC_NOISE | in_course]:
        return ""
    return name[:60].rstrip(" -,")


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
    about = topic(rec.doc, event.course if event else (hint or ""))
    if about and rec.slides:
        about = fix_umlauts(about, rec.slides())
    about = f" – {about}" if about else ""
    if event:
        start = max(start, event.start - timedelta(minutes=30))
        meta.update(course=event.course, title=f"{event.kind or 'Lecture'} {event.start:%d.%m.}{about}",
                    kind=event.kind, lecturer=event.lecturer, room=event.room,
                    calendar=event.to_meta())
        how = f"timetable ({rec.time_source})" + (" + GoodNotes folder/name" if hint else "")
        group = f"{event.course}|{event.start:%Y-%m-%dT%H:%M}"
    elif hint:
        meta.update(course=hint, title=f"Recording {start:%d.%m.}{about}" if start else rec.label)
        how = "GoodNotes folder/name" + (f", no event at {start:%d.%m. %H:%M}" if start
                                         else ", no recording time")
        group = f"{hint}|{start:%Y-%m-%d}" if start else f"rec:{rec.key}"
    else:
        meta.update(course=UNSORTED, title=rec.label)
        how = (f"no timetable event at {start:%d.%m.%Y %H:%M} ({rec.time_source})" if start
               else "no recording time found")
        group = f"rec:{rec.key}"
    if rec.doc and start and meta["course"] != UNSORTED:
        # One GoodNotes notebook = one slide deck = one topic. On block days the decks don't
        # follow the timetable slots (3_Replikation began before the 11:30 slot), so a
        # notebook's recordings of one day become one transcript, whatever the slot.
        group = f"{meta['course']}|{rec.doc}|{start:%Y-%m-%d}"
    meta["started"] = start.strftime("%Y-%m-%dT%H:%M:%S") if start else ""
    stamp = start.strftime("%Y-%m-%d %H-%M") if start else "undated"
    return {"meta": meta, "folder": f"{stamp} – {store.safe_name(meta['title'], 'Recording')}",
            "how": how, "group": group}


# ─── Transcription ───────────────────────────────────────────

def load_audio(path: str) -> np.ndarray:
    return store.decode_audio(path, SR)


def transcribe(rec: Recording, events, engine, language: str | None = "de") -> dict:
    """Transcribe one recording → a result entry (kept in state until written out)."""
    from transcriber import clean_segment              # repetition loops, silence fillers
    p = plan(rec, events)
    meta = p["meta"]
    audio = store.normalize_loudness(load_audio(rec.open()))   # quiet GoodNotes audio lost speech
    rec.duration_s = len(audio) / SR

    lang = meta.get("language") if meta.get("language") not in (None, "", "auto") else language
    # No prompt. Tested on real lectures: a course name got echoed ("M13 I. M13 I. …"); slide
    # terms, for the whole recording or just around suspected mishearings, made the model
    # invent "Vielen Dank" over real speech and rarely fixed the term.
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
    return {"key": rec.key, "source": rec.source, "doc": rec.doc, "group": p["group"],
            "folder": p["folder"], "how": p["how"], "meta": meta, "transcript_md": md,
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


def regroup(r: dict) -> str:
    """plan()'s per-notebook group for a result saved before there was one (from its source)."""
    doc = r["source"].rsplit(" › ", 1)[0] if " › " in r["source"] else ""
    doc = doc[:-len(".goodnotes")] if doc.endswith(".goodnotes") else doc     # Inbox file
    course, day = r["meta"].get("course", UNSORTED), r["meta"].get("started", "")[:10]
    if r["meta"].get("origin") != "upload" or not doc or not day or course == UNSORTED:
        return r["group"]
    return f"{course}|{doc}|{day}"


MAX_PART_GAP_S = 60 * 60     # a notebook's recordings this far apart are separate lectures


def _times(r: dict) -> tuple[datetime, datetime] | None:
    try:
        start = datetime.fromisoformat(r["meta"]["started"])
    except (KeyError, ValueError):
        return None
    return start, start + timedelta(seconds=r["meta"].get("duration_s", 0))


def lectures(results: list[dict]) -> list[list[dict]]:
    """
    Group results into lectures (same event, or same notebook that day), parts in time
    order. A notebook's recordings over an hour apart stay separate lectures; their
    group gets a suffix so each result keeps pointing at its own transcript.
    """
    groups: dict[str, list[dict]] = {}
    for r in results:
        groups.setdefault(r["group"], []).append(r)
    out = []
    for g in groups.values():
        g.sort(key=lambda r: r["meta"].get("started", ""))
        clusters = [[g[0]]]
        for r in g[1:]:
            prev, this = _times(clusters[-1][-1]), _times(r)
            if prev and this and (this[0] - prev[1]).total_seconds() > MAX_PART_GAP_S:
                clusters.append([])
            clusters[-1].append(r)
        for n, cluster in enumerate(clusters[1:], 2):
            for r in cluster:
                r["group"] += f"#{n}"
        out += clusters
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
        log=print, since="semester", only_folder: str = "", preview: bool = False,
        again: bool = False) -> dict:
    """
    preview=True only lists what was found and where it would go (Results/Preview-….txt);
    nothing is transcribed, moved or remembered.
    again=True transcribes backup recordings already done once more (after an improvement);
    their old transcript files are replaced, not kept next to the new ones.

    Transcribe everything new in <base>/Inbox and the GoodNotes backup folder(s).

    GoodNotes backs up *all* notebooks (old semesters too). From the backup, only
    recordings made since `since` are transcribed: "semester" = from two weeks before
    the timetable's first event, a datetime, or None for everything. Undated ones
    count only if their notebook name has a module code from the timetable.
    only_folder limits the backup to one GoodNotes folder. The Inbox is never filtered.

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
    for r in pending:             # saved before groups were per notebook
        if "doc" not in r:
            r["group"] = regroup(r)

    events = load_events(base)
    log(f"Timetable: {len(events)} events" if events else
        "⚠ No timetable (.ics) in the folder — sorting only by GoodNotes notebook names.")

    if since == "semester":
        since = semester_start(events)
    old = 0

    def too_old(rec: Recording) -> bool:
        """Backup recording from an earlier semester (or undated in an unrelated notebook)."""
        if since is None or rec.inbox_file or rec.meta:
            return False
        if rec.time is not None:
            return rec.time < since
        return course_hint(rec.doc or rec.label, events) is None

    skip = {k for k, v in processed.items() if not (again and "file" in v)} | {r["key"] for r in pending}
    sources = [(p, False) for p in backup_files(list(backup_folders), only_folder)] + \
              [(p, True) for p in inbox_files(inbox)]
    work = tempfile.mkdtemp(prefix="lectures-")
    todo: list[Recording] = []
    errors = []
    found: list[str] = []                         # preview lines
    empty: list[str] = []                         # notebooks without (new) recordings
    for path, is_inbox in sources:
        try:
            recs = collect(path, work, skip)
        except Exception as e:
            errors.append({"source": os.path.basename(path), "error": f"can't open: {e}"})
            continue
        root = next((b for b in backup_folders if not is_inbox and path.startswith(b)), None)
        if not recs and root:
            empty.append(os.path.relpath(path, root).replace(os.sep, "/"))
        for r in recs:
            r.inbox_file = path if is_inbox else None
            if root and r.doc:      # 'M. Sc. Psychotherapie/M13 …/3_Replikation' — folders hint the course
                r.doc = os.path.splitext(os.path.relpath(path, root))[0].replace(os.sep, "/")
                r.source = f"{r.doc} › {r.source.split(' › ')[-1]}"
            skip.add(r.key)                       # the same recording in two files → once
            if r.time is not None and too_old(r):  # GoodNotes file date: decided without unpacking
                processed[r.key] = {"skipped": "before this semester"}
                old += 1
                found.append(f"–  {r.source}   {r.time:%d.%m.%Y %H:%M} ({r.time_source}) → skip, earlier semester")
            else:
                todo.append(r)
        if is_inbox and not recs and not preview:
            shutil.move(path, os.path.join(done, os.path.basename(path)))
    if old and not preview:
        _save(processed_path, processed)
    if since:
        log(f"Only recordings since {since:%d.%m.%Y} from the GoodNotes backup"
            + (f" ({old} older ones skipped)" if old else "") + ".")
    log(f"{len(todo)} new recording(s) to check"
        + (f" ({len(pending)} already done before a disconnect)" if pending else "") + ".")

    failed_files = set()
    pending_before = len(pending)
    for n, rec in enumerate(todo, 1):
        try:
            audio_path = rec.open()
            rec.duration_s, meta_time = probe(audio_path)
            if rec.meta is None:
                if meta_time:
                    # GoodNotes stamps a recording when it *ends* (back-to-back recordings
                    # in Robin's notebooks only fit that way); other apps stamp the start.
                    gn = bool(rec.doc)
                    rec.time, rec.time_is_end = meta_time, gn
                    rec.time_source = "GoodNotes, end of recording" if gn else "audio metadata"
                elif rec.time is None and time_from_name(rec.label):
                    rec.time, rec.time_source = time_from_name(rec.label), "file name"
            if preview:
                when = f"{rec.time:%d.%m.%Y %H:%M} ({rec.time_source})" if rec.time else "no time"
                size = os.path.getsize(audio_path) / 1e6
                if too_old(rec):
                    found.append(f"–  {rec.source}   {when} → skip, earlier semester")
                else:
                    p = plan(rec, events)
                    found.append(f"{'?' if p['meta']['course'] == UNSORTED else '✓'}  {rec.source}   "
                                 f"{rec.duration_s / 60:.0f} min, {size:.1f} MB, {when}\n"
                                 f"     → {p['meta']['course']} / {p['meta']['title']}   [{p['how']}]")
                continue
            if too_old(rec):
                processed[rec.key] = {"skipped": "before this semester"}
                old += 1
            elif rec.duration_s and rec.duration_s < MIN_DURATION_S:
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
            if len(errors) == 3 and len({x["error"] for x in errors}) == 1 and \
                    len(pending) == pending_before:
                log("✗ Same error three times and nothing worked yet — a setup problem, "
                    "not the recordings. Stopping; nothing is marked as done.")
                break
        # Inbox file finished (and nothing failed) → Done. Not on a disconnect:
        # then the file stays and a rerun picks up its unfinished recordings.
        f = rec.inbox_file
        if f and f not in failed_files and os.path.exists(f) and \
                not any(r.inbox_file == f for r in todo[n:]):
            shutil.move(f, os.path.join(done, os.path.basename(f)))
    shutil.rmtree(work, ignore_errors=True)

    if preview:
        notebooks = len({s for s, inbox_ in sources if not inbox_})
        text = (f"Preview {datetime.now():%d.%m.%Y %H:%M} — nothing transcribed yet\n"
                f"{notebooks} GoodNotes notebook(s), {len(inbox_files(inbox))} Inbox file(s), "
                f"{len(found)} recording(s)" + (f" (semester from {since:%d.%m.%Y})" if since else "")
                + "\n\n" + "\n".join(found or ["No recordings found."]) + "\n"
                + (f"\nNotebooks without new recordings ({len(empty)}):\n" + "\n".join(f"   {e}" for e in empty)
                   + "\n" if empty else "")
                + "".join(f"\n✗ {e['source']}: {e['error']}" for e in errors))
        path = os.path.join(results_dir, f"Preview-{datetime.now():%Y-%m-%d-%H%M}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        log("\n" + text)
        return {"transcripts": [], "zip": None, "unsorted": 0, "failed": len(errors), "preview": path}

    if not pending:
        for e in errors:
            log(f"✗ {e['source']}: {e['error']}")
        log("Nothing new to transcribe." if not errors else "Nothing transcribed.")
        return {"transcripts": [], "zip": None, "unsorted": 0, "failed": len(errors)}

    for r in pending:             # transcribed again: the new file replaces the old one
        old = processed.get(r["key"], {}).get("file")
        if old and os.path.exists(os.path.join(base, old)):
            os.remove(os.path.join(base, old))
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


def _load_cuda_libs() -> bool:
    """Preload CUDA 12 cuBLAS + cuDNN 9 from pip's nvidia-* packages; True if loadable."""
    import ctypes
    import glob
    import site
    roots = [*site.getsitepackages(), site.getusersitepackages()]
    libs = []
    for pattern in ("libcublasLt.so.12", "libcublas.so.12", "libcudnn*.so.9"):
        libs += sorted({p for r in roots for p in glob.glob(f"{r}/nvidia/*/lib/{pattern}")})
    for _ in range(2):                        # second pass catches libs that needed a sibling first
        for lib in libs:
            try:
                ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass
    try:
        ctypes.CDLL("libcublas.so.12")
        ctypes.CDLL("libcudnn.so.9")
        return True
    except OSError:
        return False


def ensure_cuda_libs(log=print) -> None:
    """faster-whisper's GPU backend (CTranslate2) is built for CUDA 12 + cuDNN 9; newer Colab
    images ship other versions, so fetch these and load them by hand."""
    import subprocess
    import sys
    if not sys.platform.startswith("linux") or _load_cuda_libs():
        return
    log("Installing the CUDA libraries the speech model needs (about a minute)…")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                    "nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"], check=True)
    if not _load_cuda_libs():
        raise RuntimeError("CUDA 12 libraries (cuBLAS, cuDNN 9) could not be loaded")


class WhisperEngine:
    """faster-whisper on the GPU, batched (a 90-min lecture takes a few minutes on a T4)."""

    def __init__(self, model: str = "large-v3", device: str = "cuda", batch_size: int = 16,
                 vad_threshold: float | None = VAD_THRESHOLD):
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        if device == "cuda":
            ensure_cuda_libs()
        compute = "float16" if device == "cuda" else "int8"
        self.name = model
        self.batch_size = batch_size if device == "cuda" else 4
        self.vad = {"threshold": vad_threshold} if vad_threshold else None
        whisper = WhisperModel(MODELS.get(model, model), device=device, compute_type=compute)
        if device == "cuda":
            # Warm-up on noise, not silence: the VAD would drop silence and the GPU code (where
            # missing CUDA libraries show up) would first run on the real recordings.
            noise = (np.random.default_rng(0).standard_normal(SR) * 0.05).astype(np.float32)
            list(whisper.transcribe(noise, language="de", beam_size=1, vad_filter=False)[0])
        self._model = BatchedInferencePipeline(whisper)

    def transcribe(self, audio, language: str | None, prompt: str | None):
        kwargs = dict(language=language, batch_size=self.batch_size, beam_size=5)
        if self.vad:
            kwargs["vad_parameters"] = self.vad
        try:
            segments, info = self._model.transcribe(audio, initial_prompt=prompt, **kwargs)
        except TypeError:                     # older faster-whisper without prompt support
            segments, info = self._model.transcribe(audio, **kwargs)
        return [(s.start, s.end, s.text) for s in segments], info.language
