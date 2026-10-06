"""
Lecture Library — browse, search and manage recorded lectures.

  Courses │ Sessions (date, title, length, status) │ Transcript
  Search box searches every transcript; clicking a hit jumps to it.

Runs as its own process (tkinter needs a main thread).
"""

import os
import re
import sys
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import launcher
import lecture_store as store
import settings

BG = "#f3f3f3"
BG_TEXT = "#ffffff"
FG = "#1a1a1a"
FG_DIM = "#777777"
ACCENT = "#4a80c4"
BOOKMARK = "#c77700"
WARN = "#c62828"
HIGHLIGHT = "#fff3a3"

STATUS_LABELS = {
    store.STATUS_RECORDING: "● recording",
    store.STATUS_FINISHING: "… finishing",
    store.STATUS_COMPLETE: "✓",
    store.STATUS_INCOMPLETE: "⚠ gaps",
}

# Tk 8.6 can crash on characters outside the BMP (emoji) in Text.search
_NON_BMP = re.compile("[\U00010000-\U0010FFFF]")

_TS_LINE = re.compile(r"^(> 🔖 |> ⚠ )?\*\*\[(\d{2}:\d{2}:\d{2})\]\*\*\s?(.*)$")


class LibraryWindow:

    def __init__(self):
        self.root_dir = settings.get("lecture_root")
        self.courses: list[dict] = []
        self.sessions: list[dict] = []
        self.results: list[dict] = []
        self.mode = "browse"       # or "search"
        self.current: str | None = None
        self._jobs: dict[str, object] = {}

        self.win = tk.Tk()
        self.win.title("Lecture Library")
        self.win.configure(bg=BG)
        self.win.geometry("1240x720")
        self.win.minsize(800, 450)

        style = ttk.Style(self.win)
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        # ─── Search bar ─────────────────────────────────────
        top = tk.Frame(self.win, bg=BG, padx=10, pady=8)
        top.pack(fill=tk.X)
        tk.Label(top, text="🔍", bg=BG).pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.search_var, width=50)
        entry.pack(side=tk.LEFT, padx=6)
        entry.bind("<Return>", lambda e: self._search())
        ttk.Button(top, text="Search all transcripts", command=self._search).pack(side=tk.LEFT)
        ttk.Button(top, text="Clear", command=self._clear_search).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Open lectures folder",
                   command=lambda: launcher.open_path(self._ensure_root())).pack(side=tk.RIGHT)
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side=tk.RIGHT, padx=4)

        self.status = tk.Label(self.win, text="", bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
                               anchor="w", padx=10, pady=4)
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

        # ─── Panes ──────────────────────────────────────────
        panes = ttk.PanedWindow(self.win, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=10)

        left = tk.Frame(panes, bg=BG)
        tk.Label(left, text="Courses", bg=BG, fg=FG_DIM, font=("Segoe UI", 9, "bold")
                 ).pack(anchor="w")
        self.course_list = tk.Listbox(left, activestyle="none", font=("Segoe UI", 10), width=30,
                                      relief=tk.FLAT, highlightthickness=0,
                                      selectbackground=ACCENT, exportselection=False)
        self.course_list.pack(fill=tk.BOTH, expand=True)
        self.course_list.bind("<<ListboxSelect>>", lambda e: self._on_course())
        panes.add(left, weight=1)

        middle = tk.Frame(panes, bg=BG)
        self.middle_label = tk.Label(middle, text="Sessions", bg=BG, fg=FG_DIM,
                                     font=("Segoe UI", 9, "bold"))
        self.middle_label.pack(anchor="w")
        self.tree = ttk.Treeview(middle, columns=("date", "title", "length", "status"),
                                 show="headings", selectmode="browse")
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_tree_select())
        panes.add(middle, weight=2)

        right = tk.Frame(panes, bg=BG)
        self.header = tk.Label(right, text="", bg=BG, fg=FG, font=("Segoe UI", 11, "bold"),
                               anchor="w", justify=tk.LEFT, wraplength=520)
        self.header.pack(fill=tk.X)
        self.subheader = tk.Label(right, text="", bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
                                  anchor="w", justify=tk.LEFT)
        self.subheader.pack(fill=tk.X)
        actions = tk.Frame(right, bg=BG, pady=6)
        actions.pack(side=tk.BOTTOM, fill=tk.X)
        self.buttons = []
        for label, cmd in (
            ("▶ Play audio", self._play),
            ("Open transcript", self._open_transcript),
            ("Open folder", self._open_folder),
            ("Transcribe again", self._retranscribe),
            ("Rename…", self._rename),
            ("Move…", self._move),
        ):
            b = ttk.Button(actions, text=label, command=cmd)
            b.pack(side=tk.LEFT, padx=(0, 4))
            self.buttons.append(b)
        text_frame = tk.Frame(right, bg=BG)
        text_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.text = tk.Text(text_frame, wrap=tk.WORD, font=("Segoe UI", 10), bg=BG_TEXT,
                            fg=FG, relief=tk.FLAT, padx=12, pady=10, spacing3=6)
        scroll = ttk.Scrollbar(text_frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set, state=tk.DISABLED)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.text.tag_configure("ts", foreground=ACCENT, font=("Segoe UI", 9, "bold"))
        self.text.tag_configure("bookmark", foreground=BOOKMARK, font=("Segoe UI", 10, "bold"))
        self.text.tag_configure("warn", foreground=WARN)
        self.text.tag_configure("hit", background=HIGHLIGHT)

        panes.add(right, weight=4)

        self._set_browse_columns()
        self.refresh()

    # ─── Data ───────────────────────────────────────────────

    def _ensure_root(self) -> str:
        os.makedirs(self.root_dir, exist_ok=True)
        return self.root_dir

    def refresh(self):
        selected = self._selected_course()
        self.courses = store.list_courses(self.root_dir)
        self.course_list.delete(0, tk.END)
        for c in self.courses:
            n = len(store.list_sessions(c["path"]))
            self.course_list.insert(tk.END, f"{c['name']}  ({n})")
        if not self.courses:
            self._show_message("No lectures yet.\n\nStart one from the tray icon: "
                               "right-click → Start lecture…")
            return
        idx = next((i for i, c in enumerate(self.courses)
                    if selected and c["path"] == selected["path"]), 0)
        self.course_list.selection_set(idx)
        if self.mode == "browse":
            self._on_course(keep_session=True)

    def _selected_course(self) -> dict | None:
        sel = self.course_list.curselection()
        return self.courses[sel[0]] if sel and sel[0] < len(self.courses) else None

    # ─── Browse ─────────────────────────────────────────────

    def _set_browse_columns(self):
        for col, text, width in (("date", "Date", 110), ("title", "Title", 160),
                                 ("length", "Length", 60), ("status", "", 60)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=(col == "title"))

    def _on_course(self, keep_session: bool = False):
        if self.mode == "search":
            self._clear_search()
            return
        course = self._selected_course()
        previous = self.current
        self.tree.delete(*self.tree.get_children())
        self.sessions = store.list_sessions(course["path"]) if course else []
        self.middle_label.config(text=f"Sessions — {course['name']}" if course else "Sessions")
        for i, s in enumerate(self.sessions):
            self.tree.insert("", tk.END, iid=str(i), values=(
                s.get("started", "")[:16].replace("T", " "),
                s.get("title", ""),
                store.format_duration(s.get("duration_s", 0)) if s.get("duration_s") else "",
                STATUS_LABELS.get(s.get("status", ""), ""),
            ))
        if self.sessions:
            idx = next((i for i, s in enumerate(self.sessions)
                        if keep_session and s["path"] == previous), 0)
            self.tree.selection_set(str(idx))
            self.tree.see(str(idx))
        else:
            self._show_message("No sessions in this course.")

    def _on_tree_select(self):
        sel = self.tree.selection()
        if not sel:
            return
        i = int(sel[0])
        if self.mode == "browse" and i < len(self.sessions):
            self._show_session(self.sessions[i]["path"])
        elif self.mode == "search" and i < len(self.results):
            r = self.results[i]
            self._show_session(r["session_path"], jump_line=r["line"],
                               highlight=self.search_var.get().strip())

    # ─── Transcript view ────────────────────────────────────

    def _show_message(self, text: str):
        self.current = None
        self.header.config(text="")
        self.subheader.config(text="")
        self._set_text([(text, ())])
        for b in self.buttons:
            b.state(["disabled"])

    def _set_text(self, parts):
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        for content, tags in parts:
            self.text.insert(tk.END, _NON_BMP.sub("", content), tags)
        self.text.configure(state=tk.DISABLED)

    def _show_session(self, path: str, jump_line: int | None = None, highlight: str = ""):
        self.current = path
        meta = store.load_meta(path)
        for b in self.buttons:
            b.state(["!disabled"])

        self.header.config(text=f"{meta.get('title', '')} — {meta.get('course', '')}")
        bits = [meta.get("started", "")[:16].replace("T", " ")]
        if meta.get("duration_s"):
            bits.append(store.format_duration(meta["duration_s"]))
        for key in ("lecturer", "room"):
            if meta.get(key):
                bits.append(meta[key])
        langs = meta.get("detected_languages") or []
        bits.append(", ".join(langs) if langs else store.LANGUAGES.get(meta.get("language", "auto"), ""))
        status = meta.get("status", "")
        if status == store.STATUS_INCOMPLETE:
            bits.append(f"⚠ {meta.get('failed_chunks', 0)} part(s) failed — try “Transcribe again”")
        elif status in (store.STATUS_RECORDING, store.STATUS_FINISHING):
            done = store.format_duration(meta.get("transcribed_until_s", 0))
            bits.append(f"{STATUS_LABELS[status]} (text to {done})")
        if meta.get("bookmarks"):
            bits.append(f"★ {len(meta['bookmarks'])} bookmark(s)")
        self.subheader.config(text="  ·  ".join(b for b in bits if b))

        md = os.path.join(path, store.TRANSCRIPT_MD)
        try:
            with open(md, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        body_start = store._strip_front_matter(lines)

        parts = []
        line_map = {}   # md line number → text widget line
        widget_line = 1
        for n in range(body_start, len(lines)):
            line = lines[n]
            if not line.strip() or store.is_header_line(line):
                continue
            line_map[n] = widget_line
            m = _TS_LINE.match(line)
            if m:
                prefix, ts, text = m.groups()
                if prefix and "🔖" in prefix:
                    parts.append((f"★ {ts}  {text}\n", ("bookmark",)))
                elif prefix:
                    parts.append((f"⚠ {ts}  {text}\n", ("warn",)))
                else:
                    parts.append((f"{ts}  ", ("ts",)))
                    parts.append((text + "\n", ()))
            else:
                parts.append((line + "\n", ()))
            widget_line += 1
        if not parts:
            parts = [("(No transcript text yet.)", ())]
        self._set_text(parts)

        if highlight:
            start = "1.0"
            while True:
                pos = self.text.search(highlight, start, nocase=True, stopindex=tk.END)
                if not pos:
                    break
                end = f"{pos}+{len(highlight)}c"
                self.text.tag_add("hit", pos, end)
                start = end
        if jump_line is not None and jump_line in line_map:
            self.text.see(f"{line_map[jump_line]}.0")
        else:
            self.text.see("1.0")

    # ─── Search ─────────────────────────────────────────────

    def _search(self):
        query = self.search_var.get().strip()
        if not query:
            self._clear_search()
            return
        self.mode = "search"
        self.results = store.search(self.root_dir, query)
        self.tree.delete(*self.tree.get_children())
        for col, text, width in (("date", "Date", 90), ("title", "Lecture", 180),
                                 ("length", "Time", 70), ("status", "Match", 260)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=(col in ("title", "status")))
        for i, r in enumerate(self.results):
            self.tree.insert("", tk.END, iid=str(i), values=(
                r["started"][:10], f"{r['course'][:28]} – {r['title']}",
                r["timestamp"], r["snippet"],
            ))
        self.middle_label.config(text=f"{len(self.results)} matches for “{query}”")
        if self.results:
            self.tree.selection_set("0")
        else:
            self._show_message("Nothing found.")

    def _clear_search(self):
        self.mode = "browse"
        self.search_var.set("")
        self._set_browse_columns()
        self._on_course()

    # ─── Actions ────────────────────────────────────────────

    def _play(self):
        audio = store.find_audio(self.current) if self.current else None
        if audio:
            launcher.open_path(audio)
        else:
            self.status.config(text="No audio file in this session.")

    def _open_transcript(self):
        if self.current:
            launcher.open_path(os.path.join(self.current, store.TRANSCRIPT_MD))

    def _open_folder(self):
        if self.current:
            launcher.open_path(self.current)

    def _retranscribe(self):
        path = self.current
        if not path or path in self._jobs:
            return
        if store.is_session_busy(path):
            messagebox.showinfo("Still recording",
                                "This lecture is still being recorded or finished by the app.",
                                parent=self.win)
            return
        if not store.find_audio(path):
            messagebox.showwarning("No audio", "This session has no audio file.", parent=self.win)
            return
        if not messagebox.askyesno(
                "Transcribe again",
                "Re-transcribe this lecture from its audio?\n\n"
                "The current transcript will be replaced. This runs in the background "
                "and can take a while for a long lecture.", parent=self.win):
            return
        proc = launcher.spawn("lecture", "--transcribe", path)
        if proc is None:
            return
        self._jobs[path] = proc
        self.status.config(text="Transcribing in the background…")
        self.win.after(2000, lambda: self._poll_job(path))

    def _poll_job(self, path: str):
        proc = self._jobs.get(path)
        if proc is None:
            return
        meta = store.load_meta(path)
        if proc.poll() is None:
            done = store.format_duration(meta.get("transcribed_until_s", 0))
            total = store.format_duration(meta.get("duration_s", 0))
            self.status.config(text=f"Transcribing “{meta.get('title', '')}”… {done} / {total}")
            self.win.after(2000, lambda: self._poll_job(path))
            return
        del self._jobs[path]
        ok = proc.returncode == 0
        self.status.config(text=f"“{meta.get('title', '')}”: "
                                + ("transcription finished." if ok else "transcription failed — see whisper.log."))
        self.refresh()
        if self.current == path:
            self._show_session(path)

    def _rename(self):
        if not self.current:
            return
        meta = store.load_meta(self.current)
        title = simpledialog.askstring("Rename", "New title:", initialvalue=meta.get("title", ""),
                                       parent=self.win)
        if title and title.strip():
            try:
                self.current = store.rename_session(self.current, title)
            except OSError as e:
                messagebox.showerror("Rename failed", str(e), parent=self.win)
            self.refresh()

    def _move(self):
        if not self.current:
            return
        meta = store.load_meta(self.current)
        names = ", ".join(c["name"] for c in self.courses)
        course = simpledialog.askstring(
            "Move to course", f"Course name (existing: {names}):",
            initialvalue=meta.get("course", ""), parent=self.win)
        if course and course.strip() and course.strip() != meta.get("course"):
            try:
                self.current = store.move_session(self.current, self.root_dir, course.strip())
            except OSError as e:
                messagebox.showerror("Move failed", str(e), parent=self.win)
            self.refresh()

    def run(self):
        self.win.mainloop()


def main(argv: list[str]) -> int:
    LibraryWindow().run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
