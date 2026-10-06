"""
Lecture Store — on-disk organisation of lecture recordings and transcripts.

Pure logic, no audio device or GUI imports, so it can be unit-tested.

Layout:
  <root>/
    <Course>/
      course.json                         {name, language}
      2026-10-06 10-15 – Vorlesung 3/
        audio.wav | audio.mp3
        transcript.md                     YAML front matter + [HH:MM:SS] paragraphs
        transcript.srt
        lecture.json                      session metadata (see create_session)
"""

import json
import logging
import os
import re
import shutil
import struct
import threading
import time
import unicodedata

import numpy as np

logger = logging.getLogger(__name__)

COURSE_FILE = "course.json"
META_FILE = "lecture.json"
TRANSCRIPT_MD = "transcript.md"
TRANSCRIPT_SRT = "transcript.srt"
AUDIO_WAV = "audio.wav"
AUDIO_MP3 = "audio.mp3"

STATUS_RECORDING = "recording"
STATUS_FINISHING = "finishing"
STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"   # finished, but some chunks failed to transcribe

LANGUAGES = {
    "auto": "Auto-detect",
    "de":   "Deutsch",
    "en":   "English",
}

_TITLE_BASE = {"de": "Vorlesung", "en": "Lecture", "auto": "Session"}

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TS_RE = re.compile(r"\*\*\[(\d{2}):(\d{2}):(\d{2})\]\*\*")


# ─── Formatting helpers ──────────────────────────────────────

def format_ts(seconds: float) -> str:
    """Seconds → 'HH:MM:SS'."""
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def format_srt_ts(seconds: float) -> str:
    """Seconds → 'HH:MM:SS,mmm'."""
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_duration(seconds: float) -> str:
    """Seconds → '1:31:12' or '42:05'."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(text: str) -> int:
    """'HH:MM:SS' → seconds."""
    h, m, s = (int(x) for x in text.split(":"))
    return h * 3600 + m * 60 + s


def safe_name(name: str, fallback: str = "Untitled") -> str:
    """Make a string usable as a Windows folder name, keeping umlauts etc."""
    name = _INVALID_CHARS.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name[:80] or fallback


def slug(name: str) -> str:
    """'Analysis II' → 'analysis-ii' (for Markdown tags)."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "course"


def default_title_base(language: str) -> str:
    return _TITLE_BASE.get(language, _TITLE_BASE["auto"])


# ─── Metadata ────────────────────────────────────────────────

def _write_json_atomic(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_meta(session_dir: str) -> dict:
    return _read_json(os.path.join(session_dir, META_FILE))


def save_meta(session_dir: str, meta: dict):
    _write_json_atomic(os.path.join(session_dir, META_FILE), meta)


# ─── Courses ─────────────────────────────────────────────────

def course_dir(root: str, course: str) -> str:
    return os.path.join(root, safe_name(course))


def ensure_course(root: str, course: str, language: str | None = None) -> str:
    """Create the course folder (if needed) and remember its language."""
    path = course_dir(root, course)
    os.makedirs(path, exist_ok=True)
    info = _read_json(os.path.join(path, COURSE_FILE))
    info.setdefault("name", course.strip() or safe_name(course))
    if language:
        info["language"] = language
    info.setdefault("language", "auto")
    _write_json_atomic(os.path.join(path, COURSE_FILE), info)
    return path


def load_course(path: str) -> dict:
    info = _read_json(os.path.join(path, COURSE_FILE))
    info.setdefault("name", os.path.basename(path))
    info.setdefault("language", "auto")
    info["path"] = path
    return info


def list_courses(root: str) -> list[dict]:
    """All course folders under root, sorted by name."""
    if not os.path.isdir(root):
        return []
    courses = []
    for entry in os.scandir(root):
        if entry.is_dir() and not entry.name.startswith("."):
            courses.append(load_course(entry.path))
    return sorted(courses, key=lambda c: c["name"].lower())


# ─── Sessions ────────────────────────────────────────────────

def create_session(root: str, course: str, title: str, language: str,
                   model: str = "", started: float | None = None,
                   calendar: dict | None = None) -> str:
    """
    Create a new session folder with initial metadata. Returns its path.

    calendar: the matched timetable event (CalendarEvent.to_meta()), if any —
    adds kind (Vorlesung/Seminar), room, lecturer and scheduled time.
    """
    started = started or time.time()
    cdir = ensure_course(root, course)
    stamp = time.strftime("%Y-%m-%d %H-%M", time.localtime(started))
    base = f"{stamp} – {safe_name(title, 'Session')}"
    path = os.path.join(cdir, base)
    n = 2
    while os.path.exists(path):
        path = os.path.join(cdir, f"{base} ({n})")
        n += 1
    os.makedirs(path)

    meta = {
        "course": load_course(cdir)["name"],
        "title": title.strip() or "Session",
        "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "duration_s": 0.0,
        "language": language,
        "detected_languages": [],
        "model": model,
        "status": STATUS_RECORDING,
        "transcribed_until_s": 0.0,
        "failed_chunks": 0,
        "bookmarks": [],
    }
    if calendar:
        meta["kind"] = calendar.get("kind", "")
        meta["lecturer"] = calendar.get("lecturer", "")
        meta["room"] = calendar.get("room", "")
        meta["calendar"] = calendar
    save_meta(path, meta)
    return path


def list_sessions(course_path: str) -> list[dict]:
    """Sessions in a course folder, newest first. Each dict is meta + 'path'."""
    if not os.path.isdir(course_path):
        return []
    sessions = []
    for entry in os.scandir(course_path):
        if entry.is_dir() and os.path.exists(os.path.join(entry.path, META_FILE)):
            meta = load_meta(entry.path)
            meta["path"] = entry.path
            meta.setdefault("title", entry.name)
            meta.setdefault("started", "")
            sessions.append(meta)
    return sorted(sessions, key=lambda s: s.get("started", ""), reverse=True)


def next_title(course_path: str, language: str, kind: str = "") -> str:
    """
    Suggest 'Vorlesung 4' when the course already has three sessions.
    With a calendar kind ('Seminar'), numbers only sessions of that kind.
    """
    sessions = list_sessions(course_path)
    if kind:
        n = sum(1 for s in sessions if s.get("kind") == kind) + 1
        return f"{kind} {n}"
    return f"{default_title_base(language)} {len(sessions) + 1}"


def find_audio(session_dir: str) -> str | None:
    for name in (AUDIO_MP3, AUDIO_WAV):
        p = os.path.join(session_dir, name)
        if os.path.exists(p):
            return p
    return None


def find_incomplete(root: str) -> list[str]:
    """Sessions left in recording/finishing state (app crashed or was killed)."""
    found = []
    for course in list_courses(root):
        for s in list_sessions(course["path"]):
            if s.get("status") in (STATUS_RECORDING, STATUS_FINISHING):
                found.append(s["path"])
    return found


def is_session_busy(session_dir: str, idle_s: float = 30.0) -> bool:
    """True if the session is probably still being recorded right now."""
    meta = load_meta(session_dir)
    if meta.get("status") not in (STATUS_RECORDING, STATUS_FINISHING):
        return False
    wav = os.path.join(session_dir, AUDIO_WAV)
    try:
        return time.time() - os.path.getmtime(wav) < idle_s
    except OSError:
        return False


def rename_session(session_dir: str, new_title: str) -> str:
    """Change a session's title (folder name keeps its date prefix)."""
    meta = load_meta(session_dir)
    meta["title"] = new_title.strip() or meta.get("title", "Session")
    folder = os.path.basename(session_dir)
    prefix = folder.split(" – ", 1)[0] if " – " in folder else folder[:16]
    new_dir = os.path.join(os.path.dirname(session_dir),
                           f"{prefix} – {safe_name(meta['title'], 'Session')}")
    if new_dir != session_dir:
        n = 2
        base = new_dir
        while os.path.exists(new_dir):
            new_dir = f"{base} ({n})"
            n += 1
        os.rename(session_dir, new_dir)
    save_meta(new_dir, meta)
    rewrite_front_matter(new_dir, meta)
    return new_dir


def move_session(session_dir: str, root: str, new_course: str) -> str:
    """Move a session to another (possibly new) course."""
    meta = load_meta(session_dir)
    target_course = ensure_course(root, new_course)
    meta["course"] = load_course(target_course)["name"]
    new_dir = os.path.join(target_course, os.path.basename(session_dir))
    n = 2
    base = new_dir
    while os.path.exists(new_dir):
        new_dir = f"{base} ({n})"
        n += 1
    shutil.move(session_dir, new_dir)
    save_meta(new_dir, meta)
    rewrite_front_matter(new_dir, meta)
    return new_dir


# ─── Transcript files ────────────────────────────────────────

def _front_matter(meta: dict) -> str:
    date = (meta.get("started") or "")[:10]
    langs = meta.get("detected_languages") or []
    language = meta.get("language", "auto")
    if language == "auto" and langs:
        language = ", ".join(langs)

    def q(text):
        return json.dumps(str(text), ensure_ascii=False)

    lines = [
        "---",
        f"course: {q(meta.get('course', ''))}",
        f"title: {q(meta.get('title', ''))}",
        f"date: {date}",
        f"duration: {format_duration(meta.get('duration_s', 0))}",
        f"language: {language}",
        f"model: {meta.get('model', '')}",
    ]
    for key in ("kind", "lecturer", "room"):
        if meta.get(key):
            lines.append(f"{key}: {q(meta[key])}")
    cal = meta.get("calendar") or {}
    if cal.get("start"):
        lines.append(f"scheduled: {q(cal['start'][11:16] + '–' + cal.get('end', '')[11:16])}")
    tag_kind = f", {slug(meta['kind'])}" if meta.get("kind") else ""
    lines += [
        f"tags: [lecture, {slug(meta.get('course', ''))}{tag_kind}]",
        "---",
        "",
        f"# {meta.get('title', '')} — {meta.get('course', '')}",
        "",
    ]
    details = [d for d in (meta.get("lecturer"), meta.get("room")) if d]
    if details:
        lines += [f"*{' · '.join(details)} · {date}*", ""]
    return "\n".join(lines) + "\n"


def rewrite_front_matter(session_dir: str, meta: dict):
    """Replace the YAML header + title line of transcript.md, keep the body."""
    md = os.path.join(session_dir, TRANSCRIPT_MD)
    if not os.path.exists(md):
        return
    with open(md, "r", encoding="utf-8") as f:
        text = f.read()
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            body = text[end + 5:].lstrip("\n")
            if body.startswith("# "):
                body = body.split("\n", 1)[1] if "\n" in body else ""
            body = body.lstrip("\n")
            first = body.split("\n", 1)[0]
            if first.startswith("*") and not first.startswith("**") and first.endswith("*"):
                body = body.split("\n", 1)[1].lstrip("\n") if "\n" in body else ""
    tmp = md + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_front_matter(meta) + body)
    os.replace(tmp, md)


class TranscriptWriter:
    """
    Appends timestamped transcript text to transcript.md and transcript.srt.

    Segments arrive per chunk (with chunk-relative times) and are grouped into
    paragraphs of ~paragraph_s seconds. Bookmarks can be added at any time from
    another thread; they're written in timeline order before the paragraph
    that follows them.
    """

    def __init__(self, session_dir: str, paragraph_s: float = 30.0):
        self.session_dir = session_dir
        self.md_path = os.path.join(session_dir, TRANSCRIPT_MD)
        self.srt_path = os.path.join(session_dir, TRANSCRIPT_SRT)
        self.paragraph_s = paragraph_s
        self._srt_index = 0
        self._pending_bookmarks: list[tuple[float, str]] = []
        self._lock = threading.Lock()

    def write_header(self, meta: dict):
        """Start fresh files (overwrites any previous transcript)."""
        with self._lock:
            with open(self.md_path, "w", encoding="utf-8") as f:
                f.write(_front_matter(meta))
            with open(self.srt_path, "w", encoding="utf-8"):
                pass
            self._srt_index = 0

    def add_bookmark(self, t: float, label: str = "Bookmark"):
        with self._lock:
            self._pending_bookmarks.append((t, label))
            self._pending_bookmarks.sort()

    def _flush_bookmarks_before(self, t: float | None, out: list[str]):
        while self._pending_bookmarks and (t is None or self._pending_bookmarks[0][0] <= t):
            bt, label = self._pending_bookmarks.pop(0)
            out.append(f"> 🔖 **[{format_ts(bt)}]** {label}\n\n")

    def append_segments(self, segments, offset_s: float = 0.0):
        """segments: iterable of (start, end, text) relative to offset_s."""
        segs = [(offset_s + s, offset_s + e, t.strip()) for s, e, t in segments if t and t.strip()]
        if not segs:
            return

        paragraphs: list[list[tuple[float, float, str]]] = []
        for seg in segs:
            if not paragraphs or seg[0] - paragraphs[-1][0][0] >= self.paragraph_s:
                paragraphs.append([seg])
            else:
                paragraphs[-1].append(seg)

        with self._lock:
            md_out: list[str] = []
            for para in paragraphs:
                start = para[0][0]
                self._flush_bookmarks_before(start, md_out)
                text = " ".join(t for _, _, t in para)
                md_out.append(f"**[{format_ts(start)}]** {text}\n\n")

            srt_out: list[str] = []
            for s, e, t in segs:
                self._srt_index += 1
                srt_out.append(
                    f"{self._srt_index}\n{format_srt_ts(s)} --> {format_srt_ts(e)}\n{t}\n\n"
                )

            with open(self.md_path, "a", encoding="utf-8") as f:
                f.write("".join(md_out))
            with open(self.srt_path, "a", encoding="utf-8") as f:
                f.write("".join(srt_out))

    def append_note(self, t: float, note: str):
        """Write an inline note (e.g. a failed chunk) into the Markdown."""
        with self._lock:
            out: list[str] = []
            self._flush_bookmarks_before(t, out)
            out.append(f"> ⚠ **[{format_ts(t)}]** {note}\n\n")
            with open(self.md_path, "a", encoding="utf-8") as f:
                f.write("".join(out))

    def finish(self):
        """Write any bookmarks that came after the last transcribed text."""
        with self._lock:
            out: list[str] = []
            self._flush_bookmarks_before(None, out)
            if out:
                with open(self.md_path, "a", encoding="utf-8") as f:
                    f.write("".join(out))


# ─── Search ──────────────────────────────────────────────────

def _strip_front_matter(lines: list[str]) -> int:
    """Index of the first body line after YAML front matter."""
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return i + 1
    return 0


def is_header_line(line: str) -> bool:
    """The '# Title — Course' heading or the '*lecturer · room · date*' line."""
    return line.startswith("# ") or (
        line.startswith("*") and not line.startswith("**") and line.endswith("*") and len(line) > 1)


def search(root: str, query: str, limit: int = 300) -> list[dict]:
    """Case-insensitive substring search across all transcripts."""
    query = (query or "").strip().lower()
    if not query:
        return []
    results = []
    for course in list_courses(root):
        for session in list_sessions(course["path"]):
            md = os.path.join(session["path"], TRANSCRIPT_MD)
            try:
                with open(md, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            last_ts = 0
            for line_no in range(_strip_front_matter(lines), len(lines)):
                line = lines[line_no]
                if is_header_line(line):
                    continue
                m = _TS_RE.search(line)
                if m:
                    last_ts = parse_ts(":".join(m.groups()))
                idx = line.lower().find(query)
                if idx == -1:
                    continue
                text = _TS_RE.sub("", line).lstrip("> 🔖⚠").strip()
                tidx = text.lower().find(query)
                a = max(0, tidx - 25)
                b = min(len(text), tidx + len(query) + 80)
                snippet = ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")
                results.append({
                    "course": course["name"],
                    "title": session.get("title", ""),
                    "started": session.get("started", ""),
                    "session_path": session["path"],
                    "seconds": last_ts,
                    "timestamp": format_ts(last_ts),
                    "line": line_no,
                    "snippet": snippet,
                })
                if len(results) >= limit:
                    return results
    return results


# ─── Audio helpers ───────────────────────────────────────────

def find_cut_point(audio: np.ndarray, sample_rate: int, search_s: float = 5.0,
                   window_s: float = 0.3) -> int:
    """
    Pick where to end a chunk: the centre of the quietest window within the
    last `search_s` seconds of `audio`. Cutting in a pause avoids splitting
    words between two chunks, so no overlap/dedup is needed.
    """
    n = len(audio)
    win = max(1, int(window_s * sample_rate))
    start = max(0, n - int(search_s * sample_rate))
    region = audio[start:n].astype(np.float32).reshape(-1)
    if len(region) < win * 2:
        return n

    hop = max(1, win // 2)
    energy = np.square(region)
    csum = np.concatenate(([0.0], np.cumsum(energy, dtype=np.float64)))
    starts = np.arange(0, len(region) - win + 1, hop)
    window_energy = csum[starts + win] - csum[starts]
    best = int(starts[int(np.argmin(window_energy))])
    return start + best + win // 2


def split_chunks(audio: np.ndarray, sample_rate: int, chunk_s: float,
                 search_s: float = 5.0) -> list[tuple[float, np.ndarray]]:
    """Split a whole recording into (offset_s, chunk) pieces cut at pauses."""
    chunks = []
    pos = 0
    chunk_len = int(chunk_s * sample_rate)
    while len(audio) - pos > chunk_len + int(search_s * sample_rate):
        window = audio[pos:pos + chunk_len]
        cut = find_cut_point(window, sample_rate, search_s)
        chunks.append((pos / sample_rate, audio[pos:pos + cut]))
        pos += cut
    if pos < len(audio):
        chunks.append((pos / sample_rate, audio[pos:]))
    return chunks


class WavAppender:
    """
    16-bit mono WAV writer that keeps the header valid while recording,
    so a crash mid-lecture still leaves a playable file.
    """

    def __init__(self, path: str, sample_rate: int, header_every_s: float = 5.0):
        self.path = path
        self.sample_rate = sample_rate
        self.frames = 0
        self._header_every = int(header_every_s * sample_rate)
        self._since_header = 0
        self._f = open(path, "wb")
        self._f.write(_wav_header(sample_rate, 0))

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate

    def write(self, audio: np.ndarray):
        pcm = float_to_int16(audio)
        self._f.write(pcm.tobytes())
        self.frames += len(pcm)
        self._since_header += len(pcm)
        if self._since_header >= self._header_every:
            self._update_header()

    def _update_header(self):
        self._f.flush()
        pos = self._f.tell()
        self._f.seek(0)
        self._f.write(_wav_header(self.sample_rate, self.frames))
        self._f.seek(pos)
        self._f.flush()
        self._since_header = 0

    def close(self):
        if self._f is not None:
            self._update_header()
            self._f.close()
            self._f = None


def _wav_header(sample_rate: int, frames: int) -> bytes:
    data_bytes = frames * 2
    return (
        b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", data_bytes)
    )


def float_to_int16(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)


def repair_wav(path: str):
    """Fix the size fields of a WAV written by WavAppender after a crash."""
    size = os.path.getsize(path)
    if size < 44:
        return
    with open(path, "r+b") as f:
        head = f.read(44)
        if head[:4] != b"RIFF" or head[36:40] != b"data":
            return
        sample_rate = struct.unpack("<I", head[24:28])[0]
        frames = (size - 44) // 2
        f.seek(0)
        f.write(_wav_header(sample_rate, frames))


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Read a 16-bit mono WAV → (float32 audio, sample_rate)."""
    import wave
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        data = wf.readframes(wf.getnframes())
        channels = wf.getnchannels()
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, sr


def encode_mp3(wav_path: str, mp3_path: str, bitrate: int = 64):
    """WAV → MP3 with lameenc (no ffmpeg). 64 kbps mono is plenty for speech."""
    import lameenc

    audio, sr = read_wav(wav_path)
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bitrate)
    encoder.set_in_sample_rate(sr)
    encoder.set_channels(1)
    encoder.set_quality(2)
    tmp = mp3_path + ".tmp"
    with open(tmp, "wb") as f:
        step = sr * 60
        pcm = float_to_int16(audio)
        for i in range(0, len(pcm), step):
            f.write(encoder.encode(pcm[i:i + step].tobytes()))
        f.write(encoder.flush())
    os.replace(tmp, mp3_path)
