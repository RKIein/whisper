"""
Colab Exchange — move recordings to the Colab notebook and results back.

  export_for_colab(): zip every lecture not yet transcribed by Colab
                      (lecture.json + audio) → upload it to Drive/Vorlesungen/Inbox
  import_results():   take the notebook's Transkripte-<date>.zip and file it:
                      lectures recorded here get the new transcript, new ones
                      (iPad / GoodNotes) are added as transcript-only lectures.
"""

import json
import os
import zipfile

import lecture_store as store

APP_IMPORT = "app-import.json"       # written by colab_batch next to the transcripts
# Fields the notebook owns; everything else (bookmarks, title, course …) stays as you left it
RESULT_FIELDS = ("model", "transcribed_by", "status", "duration_s", "detected_languages",
                 "transcribed_until_s", "failed_chunks")


def pending_sessions(root: str) -> list[dict]:
    """Lectures with audio that Colab hasn't transcribed yet (and aren't recording)."""
    out = []
    for course in store.list_courses(root):
        for s in store.list_sessions(course["path"]):
            if s.get("transcribed_by") == "colab" or store.is_session_busy(s["path"]):
                continue
            if store.find_audio(s["path"]):
                out.append(s)
    return out


def export_for_colab(root: str, dest_zip: str) -> int:
    """Write the upload zip. Returns how many lectures it contains."""
    sessions = pending_sessions(root)
    if not sessions:
        return 0
    tmp = dest_zip + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:   # mp3 doesn't compress
        for s in sessions:
            course = os.path.basename(os.path.dirname(s["path"]))
            folder = f"{course}/{os.path.basename(s['path'])}"
            z.write(os.path.join(s["path"], store.META_FILE), f"{folder}/{store.META_FILE}")
            audio = store.find_audio(s["path"])
            z.write(audio, f"{folder}/{os.path.basename(audio)}")
    os.replace(tmp, dest_zip)
    return len(sessions)


def _find_existing(root: str, course: str, folder: str, started: str) -> str | None:
    """Same course/folder, or — if you renamed or moved it since — the same start time."""
    path = os.path.join(store.course_dir(root, course), folder)
    if os.path.exists(os.path.join(path, store.META_FILE)):
        return path
    if started:
        for c in store.list_courses(root):
            for s in store.list_sessions(c["path"]):
                if s.get("started") == started:
                    return s["path"]
    return None


def import_results(root: str, zip_path: str) -> tuple[int, int]:
    """File the notebook's transcripts into the library. Returns (new, updated)."""
    with zipfile.ZipFile(zip_path) as z:
        entries = json.loads(z.read(APP_IMPORT).decode("utf-8"))
    new = updated = 0
    for entry in entries:
        meta, folder, md = entry["meta"], entry["folder"], entry["transcript_md"]
        existing = None
        if meta.get("origin") == "app":     # never let an iPad copy overwrite a laptop recording
            existing = _find_existing(root, meta.get("course", ""), folder, meta.get("started", ""))
        if existing:
            # Recorded here: keep your title, course and bookmarks, swap in the better transcript
            local = store.load_meta(existing)
            local.update({k: meta[k] for k in RESULT_FIELDS if k in meta})
            with open(os.path.join(existing, store.TRANSCRIPT_MD), "w", encoding="utf-8") as f:
                f.write(md)
            store.save_meta(existing, local)
            store.rewrite_front_matter(existing, local)
            updated += 1
        else:                                # iPad / GoodNotes: a new lecture (transcript only)
            cdir = store.ensure_course(root, meta.get("course") or "Unsorted")
            dest, n = os.path.join(cdir, folder), 2
            while os.path.exists(dest):
                dest, n = os.path.join(cdir, f"{folder} ({n})"), n + 1
            os.makedirs(dest)
            store.save_meta(dest, meta)
            with open(os.path.join(dest, store.TRANSCRIPT_MD), "w", encoding="utf-8") as f:
                f.write(md)
            new += 1
    return new, updated
