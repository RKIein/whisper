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
from datetime import datetime
from tkinter import messagebox, simpledialog

import launcher
import lecture_store as store
import settings
import ui

STATUS_LABELS = {
    store.STATUS_RECORDING: "● recording",
    store.STATUS_FINISHING: "… finishing",
    store.STATUS_COMPLETE: "✓",
    store.STATUS_INCOMPLETE: "⚠ gaps",
}
STATUS_COLORS = {
    store.STATUS_RECORDING: ui.RECORD,
    store.STATUS_FINISHING: ui.BOOKMARK,
    store.STATUS_COMPLETE: ui.OK,
    store.STATUS_INCOMPLETE: ui.WARN,
}

# Tk 8.6 can crash on characters outside the BMP (emoji) in Text.search
_NON_BMP = re.compile("[\U00010000-\U0010FFFF]")

_TS_LINE = re.compile(r"^(> 🔖 |> ⚠ )?\*\*\[(\d{2}:\d{2}:\d{2})\]\*\*\s?(.*)$")

SEARCH_DELAY_MS = 400
RESULTS_PAGE = 40


def _when(started: str) -> str:
    """'2026-10-06T14:02:00' → 'Mon 06.10.2026  ·  14:02'."""
    try:
        return f"{datetime.fromisoformat(started):%a %d.%m.%Y  ·  %H:%M}"
    except ValueError:
        return started[:16].replace("T", " ")


class LibraryWindow:

    def __init__(self):
        self.root_dir = settings.get("lecture_root")
        self.courses: list[dict] = []
        self.sessions: list[dict] = []
        self.results: list[dict] = []
        self.mode = "browse"       # or "search"
        self.current: str | None = None
        self._jobs: dict[str, object] = {}
        self._search_job = None

        self.win = ui.window("Lecture Library", resizable=True)
        self.win.geometry("1180x720")
        self.win.minsize(820, 450)

        # ─── Search bar (seamless, as in history) ───────────
        top = tk.Frame(self.win, bg=ui.BG, padx=20, pady=14)
        top.pack(fill=tk.X)
        self.search = ui.SearchField(top, "Search all lectures")
        self.search.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=2)
        self.search.bind("<Return>", lambda e: self._search())
        self.search.bind("<Escape>", lambda e: self._clear_search())
        self.search.var.trace_add("write", lambda *_: self._schedule_search())
        self.clear_link = ui.Link(top, "✕", self._clear_search, size=10, fg=ui.FG_DIM)
        ui.Link(top, "Refresh", self.refresh, size=10).pack(side=tk.RIGHT, padx=(18, 0))
        ui.Link(top, "Open lectures folder", lambda: launcher.open_path(self._ensure_root()),
                size=10).pack(side=tk.RIGHT, padx=(18, 0))

        self.status = tk.Label(self.win, text="", bg=ui.BG, fg=ui.FG_DIM, font=(ui.FONT, 9),
                               anchor="w", padx=20, pady=6)
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

        # ─── Panes ──────────────────────────────────────────
        panes = tk.PanedWindow(self.win, orient=tk.HORIZONTAL, bg=ui.BG, bd=0,
                               sashwidth=10, sashrelief=tk.FLAT, showhandle=False)
        panes.pack(fill=tk.BOTH, expand=True, padx=(12, 20))

        left = tk.Frame(panes, bg=ui.BG)
        ui.caption(left, "Courses").pack(fill=tk.X, padx=12, pady=(0, 4))
        self.course_list = ui.CardList(left, self._build_course, self._on_course_click,
                                       card_bg=ui.BG, padx=12, pady=7, gap=0)
        self.course_list.pack(fill=tk.BOTH, expand=True)
        panes.add(left, minsize=180, width=250)

        middle = tk.Frame(panes, bg=ui.BG)
        self.middle_label = ui.caption(middle, "Sessions")
        self.middle_label.pack(fill=tk.X, pady=(0, 4))
        self.session_list = ui.CardList(middle, self._build_card, self._on_card_select)
        self.session_list.pack(fill=tk.BOTH, expand=True)
        panes.add(middle, minsize=240, width=330)

        right = tk.Frame(panes, bg=ui.BG)
        self.header = tk.Label(right, text="", bg=ui.BG, fg=ui.FG, font=(ui.FONT, 13, "bold"),
                               anchor="w", justify=tk.LEFT, wraplength=520)
        self.header.pack(fill=tk.X, padx=(8, 0))
        self.subheader = tk.Label(right, text="", bg=ui.BG, fg=ui.FG_DIM, font=(ui.FONT, 9),
                                  anchor="w", justify=tk.LEFT, wraplength=520)
        self.subheader.pack(fill=tk.X, padx=(8, 0))
        right.bind("<Configure>", lambda e: (self.header.config(wraplength=e.width - 20),
                                             self.subheader.config(wraplength=e.width - 20)))

        actions = tk.Frame(right, bg=ui.BG)
        actions.pack(fill=tk.X, padx=(8, 0), pady=(8, 8))
        self.buttons = []
        for label, cmd in (
            ("▶  Play audio", self._play),
            ("Open transcript", self._open_transcript),
            ("Open folder", self._open_folder),
            ("More  ▾", self._more),
        ):
            b = ui.Link(actions, label, cmd, size=10)
            b.pack(side=tk.LEFT, padx=(0, 18))
            self.buttons.append(b)

        text_frame = tk.Frame(right, bg=ui.BG_ENTRY)
        text_frame.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(text_frame, wrap=tk.WORD, font=(ui.FONT, 10), bg=ui.BG_ENTRY,
                            fg=ui.FG, relief=tk.FLAT, bd=0, highlightthickness=0,
                            padx=18, pady=14, spacing3=8, cursor="arrow")
        scroll = ui.PillScrollbar(text_frame, command=self.text.yview, bg=ui.BG_ENTRY)
        self.text.configure(yscrollcommand=scroll.set, state=tk.DISABLED)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.text.tag_configure("ts", foreground=ui.ACCENT, font=(ui.FONT, 9))
        self.text.tag_configure("bookmark", foreground=ui.BOOKMARK, font=(ui.FONT, 10, "bold"))
        self.text.tag_configure("warn", foreground=ui.WARN)
        self.text.tag_configure("hit", background=ui.HIGHLIGHT)
        self.text.tag_configure("message", foreground=ui.FG_DIM, justify=tk.CENTER)

        panes.add(right, minsize=320)

        self.refresh()

    # ─── Data ───────────────────────────────────────────────

    def _ensure_root(self) -> str:
        os.makedirs(self.root_dir, exist_ok=True)
        return self.root_dir

    def refresh(self):
        selected = self._selected_course()
        self.courses = store.list_courses(self.root_dir)
        for c in self.courses:
            c["count"] = len(store.list_sessions(c["path"]))
        self.course_list.set_items(self.courses, empty_text="No courses yet")
        if not self.courses:
            self.session_list.set_items([])
            self._show_message("No lectures yet.\n\nStart one from the tray icon:\n"
                               "right-click → Start lecture…")
            return
        # Follow the open session (e.g. after “Move to…”), else keep the course
        wanted = os.path.dirname(self.current) if self.current else (selected or {}).get("path")
        idx = next((i for i, c in enumerate(self.courses)
                    if os.path.normcase(c["path"]) == os.path.normcase(wanted or "")), 0)
        self.course_list.select(idx, notify=False)
        if self.mode == "browse":
            self._on_course(keep_session=True)
        else:
            self._search()

    def _selected_course(self) -> dict | None:
        i = self.course_list.selected
        return self.courses[i] if i is not None and i < len(self.courses) else None

    # ─── Cards ──────────────────────────────────────────────

    def _build_course(self, card, course):
        tk.Label(card, text=str(course.get("count", "")), font=(ui.FONT, 9), fg=ui.FG_DIM
                 ).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Label(card, text=course["name"], font=(ui.FONT, 10), fg=ui.FG, anchor="w",
                 justify=tk.LEFT, wraplength=190).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _build_card(self, card, item):
        meta = tk.Frame(card)
        meta.pack(fill=tk.X, pady=(0, 2))
        if self.mode == "search":
            info = f"{item['course']}  ·  {item['started'][:10]}  ·  {item['timestamp']}"
            tk.Label(meta, text=info, font=(ui.FONT, 9), fg=ui.FG_DIM, anchor="w").pack(side=tk.LEFT)
            tk.Label(card, text=item["title"], font=(ui.FONT, 10, "bold"), fg=ui.FG,
                     anchor="w").pack(fill=tk.X)
            tk.Label(card, text=_NON_BMP.sub("", item["snippet"]), font=(ui.FONT, 10), fg=ui.FG,
                     anchor="w", justify=tk.LEFT, wraplength=280).pack(fill=tk.X)
            return
        bits = [_when(item.get("started", ""))]
        if item.get("duration_s"):
            bits.append(store.format_duration(item["duration_s"]))
        tk.Label(meta, text="  ·  ".join(bits), font=(ui.FONT, 9), fg=ui.FG_DIM,
                 anchor="w").pack(side=tk.LEFT)
        status = item.get("status", "")
        if status in STATUS_LABELS:
            tk.Label(meta, text=STATUS_LABELS[status], font=(ui.FONT, 9),
                     fg=STATUS_COLORS[status]).pack(side=tk.RIGHT)
        tk.Label(card, text=item.get("title", ""), font=(ui.FONT, 10), fg=ui.FG, anchor="w",
                 justify=tk.LEFT, wraplength=280).pack(fill=tk.X)

    # ─── Browse ─────────────────────────────────────────────

    def _on_course_click(self, _index: int):
        if self.mode == "search":
            self._clear_search()
        else:
            self._on_course()

    def _on_course(self, keep_session: bool = False):
        course = self._selected_course()
        previous = self.current
        self.sessions = store.list_sessions(course["path"]) if course else []
        self.middle_label.config(text=course["name"] if course else "Sessions")
        self.session_list.set_items(self.sessions, empty_text="No sessions in this course.")
        if self.sessions:
            idx = next((i for i, s in enumerate(self.sessions)
                        if keep_session and s["path"] == previous), 0)
            self.session_list.select(idx)
        else:
            self._show_message("No sessions in this course.")

    def _on_card_select(self, i: int):
        if self.mode == "browse" and i < len(self.sessions):
            self._show_session(self.sessions[i]["path"])
        elif self.mode == "search" and i < len(self.results):
            r = self.results[i]
            self._show_session(r["session_path"], jump_line=r["line"],
                               highlight=self.search.value())

    # ─── Transcript view ────────────────────────────────────

    def _show_message(self, text: str):
        self.current = None
        self.header.config(text="")
        self.subheader.config(text="")
        self._set_text([("\n\n" + text, ("message",))])
        for b in self.buttons:
            b.enable(False)

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
            b.enable(True)

        self.header.config(text=meta.get("title", ""))
        bits = [meta.get("course", ""), _when(meta.get("started", ""))]
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
                    parts.append((f"{ts}   ", ("ts",)))
                    parts.append((text + "\n", ()))
            else:
                parts.append((line + "\n", ()))
            widget_line += 1
        if not parts:
            parts = [("\n\n(No transcript text yet.)", ("message",))]
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

    def _schedule_search(self):
        """Search as you type (after a short pause)."""
        if self._search_job is not None:
            self.win.after_cancel(self._search_job)
            self._search_job = None
        query = self.search.value()
        if len(query) >= 2:
            self._search_job = self.win.after(SEARCH_DELAY_MS, self._search)
        elif not query and self.mode == "search":
            self._search_job = self.win.after(SEARCH_DELAY_MS, self._clear_search)

    def _search(self):
        self._search_job = None
        query = self.search.value()
        if not query:
            self._clear_search()
            return
        self.mode = "search"
        self.clear_link.pack(side=tk.LEFT, padx=(8, 0))
        self.results = store.search(self.root_dir, query)
        n = len(self.results)
        self.middle_label.config(text=f"{n} match{'es' if n != 1 else ''} for “{query}”")
        self.session_list.set_items(self.results[:RESULTS_PAGE], empty_text="Nothing found.")
        self._show_more_link()
        if self.results:
            self.session_list.select(0)
        else:
            self._show_message("Nothing found.")

    def _show_more_link(self):
        """Cards are slow to build in bulk — show results a page at a time."""
        shown = len(self.session_list.cards)
        if shown < len(self.results):
            ui.Link(self.session_list.inner, f"Show more  ({len(self.results) - shown} left)",
                    self._show_more, size=10).pack(pady=12)

    def _show_more(self):
        shown = len(self.session_list.cards)
        self.session_list.inner.winfo_children()[-1].destroy()
        self.session_list.add_items(self.results[shown:shown + RESULTS_PAGE])
        self._show_more_link()

    def _clear_search(self):
        if self._search_job is not None:
            self.win.after_cancel(self._search_job)
            self._search_job = None
        self.mode = "browse"
        self.clear_link.pack_forget()
        if self.search.value():
            self.search.clear()
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

    def _more(self):
        """Less frequent actions; “Move to” lists the courses."""
        if not self.current:
            return
        current_course = store.load_meta(self.current).get("course", "")
        menu = ui.popup_menu(self.win)
        menu.add_command(label="Transcribe again…", command=self._retranscribe)
        menu.add_command(label="Rename…", command=self._rename)
        move = ui.popup_menu(menu)
        for c in self.courses:
            if c["name"] != current_course:
                move.add_command(label=c["name"], command=lambda n=c["name"]: self._move_to(n))
        if move.index(tk.END) is not None:
            move.add_separator()
        move.add_command(label="New course…", command=self._move_to_new)
        menu.add_cascade(label="Move to course", menu=move)
        link = self.buttons[-1]
        menu.tk_popup(link.winfo_rootx(), link.winfo_rooty() + link.winfo_height())

    def _move_to_new(self):
        course = simpledialog.askstring("Move to new course", "Course name:", parent=self.win)
        if course and course.strip():
            self._move_to(course.strip())

    def _move_to(self, course: str):
        if not self.current or course == store.load_meta(self.current).get("course"):
            return
        try:
            self.current = store.move_session(self.current, self.root_dir, course)
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
