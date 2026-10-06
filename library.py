"""
Lecture Library — browse, search and manage recorded lectures.

  Sidebar: search + lectures grouped by course │ Reading view: transcript
  The search box searches every transcript; clicking a hit jumps to it.

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
from audio_player import AudioPlayer
import settings
import ui

STATUS_MARKS = {
    store.STATUS_RECORDING: ("●", ui.RECORD),
    store.STATUS_FINISHING: ("…", ui.BOOKMARK),
    store.STATUS_COMPLETE: ("✓", ui.OK),
    store.STATUS_INCOMPLETE: ("⚠", ui.WARN),
}

# Tk 8.6 can crash on characters outside the BMP (emoji) in Text.search
_NON_BMP = re.compile("[\U00010000-\U0010FFFF]")

_TS_LINE = re.compile(r"^(> 🔖 |> ⚠ )?\*\*\[(\d{2}:\d{2}:\d{2})\]\*\*\s?(.*)$")

# TraiNex names look like "M13 I Multivariate Verfahren, …" — module code first
_COURSE_CODE = re.compile(r"^(M\d+\w*)\s+(?:I\s+)?(.+)$")

BETTER_MODEL = "large-v3-turbo"
SEARCH_DELAY_MS = 400
RESULTS_PAGE = 40
SIDEBAR_W = 330
HEADING_HOVER = "#e8e8e8"
NOW_BG = "#e8f0fe"     # the paragraph that is playing right now
SPEEDS = (1.0, 1.25, 1.5, 1.75, 2.0)
READING_W = 760       # max width of the transcript text, for comfortable reading


def _course_order(name: str):
    """M4 before M13 (by number), courses without a code after them."""
    code, rest = _course_parts(name)
    digits = re.sub(r"\D", "", code)
    return (0, int(digits), rest.lower()) if digits else (1, 0, name.lower())


def _clickable(frame, command):
    """Whole row clickable, with a hover shade (Leave also fires over child labels)."""
    def inside(e):
        w = frame.winfo_containing(e.x_root, e.y_root)
        while w is not None and w is not frame:
            w = w.master
        return w is frame
    for w in (frame, *frame.winfo_children()):
        w.bind("<Button-1>", lambda e: command())
        w.configure(cursor="hand2")
    frame.bind("<Enter>", lambda e: ui.set_bg(frame, HEADING_HOVER))
    frame.bind("<Leave>", lambda e: None if inside(e) else ui.set_bg(frame, ui.BG))


def _course_parts(name: str) -> tuple[str, str]:
    """'M13 I Multivariate Verfahren' → ('M13', 'Multivariate Verfahren')."""
    m = _COURSE_CODE.match(name)
    return (m.group(1), m.group(2)) if m else ("", name)


def _short_course(name: str) -> str:
    code, rest = _course_parts(name)
    return code or rest


def _date(started: str, with_time: bool = False) -> str:
    """'2026-10-06T14:02:00' → 'Tue 06.10.2026' (· 14:02)."""
    try:
        d = datetime.fromisoformat(started)
    except ValueError:
        return started[:16].replace("T", " ")
    return f"{d:%a %d.%m.%Y}" + (f", {d:%H:%M}" if with_time else "")


def _minutes(seconds: float) -> str:
    """5400 → '1 h 30 min', 2324 → '39 min'."""
    m = int(round(seconds / 60))
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{max(m, 1)} min"


class LibraryWindow:

    def __init__(self):
        self.root_dir = settings.get("lecture_root")
        self.courses: list[dict] = []
        self.rows: list[dict] = []       # course headings and sessions, as shown
        self.results: list[dict] = []
        self.mode = "browse"             # or "search"
        self.current: str | None = None
        self._jobs: dict[str, object] = {}
        self._search_job = None
        saved = settings.get("library_open_courses")
        self._open_courses: set[str] | None = set(saved) if saved is not None else None
        self.player = AudioPlayer()
        self._line_secs: dict[int, int] = {}     # transcript line → seconds into the audio
        self._now_line: int | None = None

        self.win = ui.window("Lecture Library", resizable=True)
        px = ui.px
        self.win.geometry(f"{px(1180)}x{px(740)}")
        self.win.minsize(px(820), px(480))

        panes = tk.PanedWindow(self.win, orient=tk.HORIZONTAL, bg=ui.BORDER, bd=0,
                               sashwidth=1, sashrelief=tk.FLAT, showhandle=False)
        panes.pack(fill=tk.BOTH, expand=True)

        # ─── Sidebar ────────────────────────────────────────
        side = tk.Frame(panes, bg=ui.BG)
        top = tk.Frame(side, bg=ui.BG, padx=px(18), pady=px(16))
        top.pack(fill=tk.X)
        self.search = ui.SearchField(top, "Search lectures")
        self.search.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=px(2))
        self.search.bind("<Return>", lambda e: self._search())
        self.search.bind("<Escape>", lambda e: self._clear_search())
        self.search.var.trace_add("write", lambda *_: self._schedule_search())
        self.clear_link = ui.Link(top, "✕", self._clear_search, size=10, fg=ui.FG_DIM)

        self.list_caption = ui.caption(side, "")
        self.list = ui.CardList(side, self._build_row, self._on_select,
                                selectable=lambda r: r["type"] != "course",
                                padx=14, pady=8, gap=2)

        foot = tk.Frame(side, bg=ui.BG, padx=px(18), pady=px(12))
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        ui.Link(foot, "Open lectures folder", lambda: launcher.open_path(self._ensure_root())
                ).pack(side=tk.LEFT)
        ui.Link(foot, "Refresh", self.refresh).pack(side=tk.LEFT, padx=(px(16), 0))
        self.list.pack(fill=tk.BOTH, expand=True, padx=(px(10), 0))
        panes.add(side, minsize=px(240), width=px(SIDEBAR_W))

        # ─── Reading view ───────────────────────────────────
        read = tk.Frame(panes, bg=ui.BG, padx=px(28), pady=px(20))
        self.title = tk.Label(read, bg=ui.BG, fg=ui.FG, font=(ui.FONT, 17, "bold"),
                              anchor="w", justify=tk.LEFT)
        self.title.pack(fill=tk.X)
        self.meta = tk.Label(read, bg=ui.BG, fg=ui.FG_DIM, font=(ui.FONT, 10),
                             anchor="w", justify=tk.LEFT)
        self.meta.pack(fill=tk.X, pady=(px(2), 0))
        self.notice = tk.Label(read, bg=ui.BG, font=(ui.FONT, 10), anchor="w", justify=tk.LEFT)
        read.bind("<Configure>", lambda e: [w.config(wraplength=e.width - px(60))
                                            for w in (self.title, self.meta, self.notice)])

        self.actions = tk.Frame(read, bg=ui.BG)
        self.actions.pack(fill=tk.X, pady=(px(14), px(14)))
        self.more = ui.Link(self.actions, "More  ▾", self._more, size=10)
        self.more.pack(side=tk.RIGHT, padx=(px(16), 0))
        self.buttons = [ui.Button(self.actions, "Open transcript", self._open_transcript,
                                  kind="plain")]
        self.buttons[0].pack(side=tk.RIGHT, padx=(px(8), 0))

        # Player: ▶  12:34 ━━━━●──── 38:44  1×
        bar = tk.Frame(self.actions, bg=ui.BG_ENTRY, padx=px(10), pady=px(5))
        bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.play_btn = tk.Label(bar, text="▶", font=(ui.FONT, 11), fg="#ffffff", bg=ui.ACCENT,
                                 width=3, cursor="hand2")
        self.play_btn.pack(side=tk.LEFT)
        self.play_btn.bind("<Button-1>", lambda e: self._toggle_play())
        self.play_btn.bind("<Enter>", lambda e: self.play_btn.config(bg=ui.ACCENT_HOVER))
        self.play_btn.bind("<Leave>", lambda e: self.play_btn.config(bg=ui.ACCENT))
        self.pos_label = tk.Label(bar, text="0:00", font=(ui.FONT, 9), fg=ui.FG_DIM,
                                  bg=ui.BG_ENTRY, width=7)
        self.pos_label.pack(side=tk.LEFT, padx=(px(6), 0))
        self.speed_link = ui.Link(bar, "1×", self._cycle_speed, bg=ui.BG_ENTRY, size=10)
        self.speed_link.pack(side=tk.RIGHT, padx=(px(8), px(2)))
        self.dur_label = tk.Label(bar, text="0:00", font=(ui.FONT, 9), fg=ui.FG_DIM,
                                  bg=ui.BG_ENTRY, width=7)
        self.dur_label.pack(side=tk.RIGHT)
        self.slider = ui.Slider(bar, self._on_seek)
        self.slider.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.status = tk.Label(read, text="", bg=ui.BG, fg=ui.FG_DIM, font=(ui.FONT, 9),
                               anchor="w")
        self.status.pack(side=tk.BOTTOM, fill=tk.X, pady=(px(8), 0))

        page = tk.Frame(read, bg=ui.BG_ENTRY)
        page.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(page, wrap=tk.WORD, font=(ui.FONT, 11), bg=ui.BG_ENTRY, fg=ui.FG,
                            relief=tk.FLAT, bd=0, highlightthickness=0, padx=px(32), pady=px(24),
                            spacing1=px(2), spacing2=px(3), spacing3=px(12), cursor="arrow")
        scroll = ui.PillScrollbar(page, command=self.text.yview, bg=ui.BG_ENTRY)
        self.text.configure(yscrollcommand=scroll.set, state=tk.DISABLED)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Keep lines at a readable length on wide windows
        self.text.bind("<Configure>", lambda e: self.text.config(
            padx=max(px(32), (e.width - px(READING_W)) // 2)))
        self.text.tag_configure("ts", foreground=ui.ACCENT, font=(ui.FONT, 9))
        self.text.tag_configure("bookmark", foreground=ui.BOOKMARK, font=(ui.FONT, 11, "bold"))
        self.text.tag_configure("warn", foreground=ui.WARN)
        self.text.tag_configure("hit", background=ui.HIGHLIGHT)
        self.text.tag_configure("message", foreground=ui.FG_DIM, justify=tk.CENTER)
        self.text.tag_configure("now", background=NOW_BG)
        self.text.tag_lower("now")
        # Click a timestamp (or a bookmark) to play from there
        self.text.tag_bind("jump", "<Button-1>", self._on_jump_click)
        self.text.tag_bind("jump", "<Enter>", lambda e: self.text.config(cursor="hand2"))
        self.text.tag_bind("jump", "<Leave>", lambda e: self.text.config(cursor="arrow"))
        panes.add(read, minsize=px(420))

        self.win.bind("<Control-f>", lambda e: self.search.focus_set())
        self.win.bind("<space>", lambda e: self._key(self._toggle_play))
        self.win.bind("<Left>", lambda e: self._key(lambda: self._skip(-10)))
        self.win.bind("<Right>", lambda e: self._key(lambda: self._skip(10)))
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        self.refresh()
        self._tick()

    # ─── Data ───────────────────────────────────────────────

    def _ensure_root(self) -> str:
        os.makedirs(self.root_dir, exist_ok=True)
        return self.root_dir

    def refresh(self):
        self.courses = store.list_courses(self.root_dir)
        if self.mode == "search":
            self._search()
        else:
            self._show_browse()

    def _show_browse(self, keep_scroll: bool = False):
        courses = [(c, store.list_sessions(c["path"])) for c in self.courses]
        courses = sorted(((c, s) for c, s in courses if s), key=lambda cs: _course_order(cs[0]["name"]))
        if self._open_courses is None:
            # First time: open the course with the most recent lecture
            newest = max(courses, key=lambda cs: cs[1][0].get("started", ""), default=None)
            self._open_courses = {newest[0]["name"]} if newest else set()
        self.rows = []
        for c, sessions in courses:
            is_open = c["name"] in self._open_courses
            self.rows.append({"type": "course", **c, "count": len(sessions), "open": is_open})
            if is_open:
                self.rows += [{"type": "session", **s} for s in sessions]

        y = self.list.canvas.yview()[0]
        self._set_caption("")
        self.list.set_items(self.rows, empty_text="No lectures yet")
        if not self.rows:
            self._show_message("No lectures yet.\n\nStart one from the tray icon:\n"
                               "right-click → Start lecture…")
            return
        if keep_scroll:
            self.win.update_idletasks()
            self.list.canvas.yview_moveto(y)
        idx = next((i for i, r in enumerate(self.rows)
                    if r["type"] == "session" and r["path"] == self.current), None)
        if idx is not None:
            self.list.select(idx, notify=not keep_scroll, scroll=not keep_scroll)
        elif self.current is None and self.list.first() is not None:
            self.list.select(self.list.first())

    def _toggle_course(self, name: str):
        self._open_courses ^= {name}
        settings.put("library_open_courses", sorted(self._open_courses))
        self._show_browse(keep_scroll=True)

    def _set_caption(self, text: str):
        if text:
            self.list_caption.config(text=text, padx=ui.px(18))
            self.list_caption.pack(fill=tk.X, pady=(0, ui.px(6)), before=self.list)
        else:
            self.list_caption.pack_forget()

    # ─── Sidebar rows ───────────────────────────────────────

    def _build_row(self, frame, row):
        px = ui.px
        if row["type"] == "course":
            code, rest = _course_parts(row["name"])
            frame.config(padx=px(8), pady=px(8), cursor="hand2")
            frame.pack_configure(pady=(px(2), px(2) if row["open"] else 0))
            tk.Label(frame, text="▾" if row["open"] else "▸", font=(ui.FONT, 10), fg=ui.FG_DIM,
                     width=2, anchor="w").pack(side=tk.LEFT, anchor="n")
            if code:
                tk.Label(frame, text=code, font=(ui.FONT, 10, "bold"), fg=ui.FG, anchor="w"
                         ).pack(side=tk.LEFT, anchor="n")
            tk.Label(frame, text=str(row["count"]), font=(ui.FONT, 9), fg=ui.FG_DIM
                     ).pack(side=tk.RIGHT, anchor="n", padx=(px(8), px(6)))
            tk.Label(frame, text=rest, font=(ui.FONT, 9), fg=ui.FG_DIM, anchor="w",
                     justify=tk.LEFT, wraplength=px(SIDEBAR_W - 150)
                     ).pack(side=tk.LEFT, padx=(px(6) if code else 0, 0), fill=tk.X, pady=(px(1), 0))
            _clickable(frame, lambda: self._toggle_course(row["name"]))
            return
        if row["type"] == "hit":
            info = f"{_short_course(row['course'])}  ·  {row['title']}  ·  {row['timestamp']}"
            tk.Label(frame, text=info, font=(ui.FONT, 9), fg=ui.FG_DIM, anchor="w"
                     ).pack(fill=tk.X)
            tk.Label(frame, text=_NON_BMP.sub("", row["snippet"]), font=(ui.FONT, 10), fg=ui.FG,
                     anchor="w", justify=tk.LEFT, wraplength=px(SIDEBAR_W - 70)).pack(fill=tk.X)
            return
        head = tk.Frame(frame)
        head.pack(fill=tk.X)
        tk.Label(head, text=row.get("title", ""), font=(ui.FONT, 10, "bold"), fg=ui.FG,
                 anchor="w").pack(side=tk.LEFT)
        mark, color = STATUS_MARKS.get(row.get("status", ""), ("", ui.FG_DIM))
        if mark:
            tk.Label(head, text=mark, font=(ui.FONT, 10), fg=color).pack(side=tk.RIGHT)
        bits = [_date(row.get("started", ""))]
        if row.get("duration_s"):
            bits.append(_minutes(row["duration_s"]))
        tk.Label(frame, text="  ·  ".join(bits), font=(ui.FONT, 9), fg=ui.FG_DIM, anchor="w"
                 ).pack(fill=tk.X)

    def _on_select(self, i: int):
        if self.mode == "search":
            if i < len(self.results):
                r = self.results[i]
                self._show_session(r["session_path"], jump_line=r["line"],
                                   highlight=self.search.value())
        elif i < len(self.rows) and self.rows[i]["type"] == "session":
            self._show_session(self.rows[i]["path"])

    # ─── Reading view ───────────────────────────────────────

    def _enable_actions(self, on: bool):
        for b in self.buttons:
            b.config(state=tk.NORMAL if on else tk.DISABLED)
        self.more.enable(on)
        if on:
            self.actions.pack(fill=tk.X, pady=(ui.px(14), ui.px(14)), after=self.meta)
        else:
            self.actions.pack_forget()

    def _show_message(self, text: str):
        self._reset_player(0)
        self.current = None
        self.title.config(text="")
        self.meta.config(text="")
        self.notice.pack_forget()
        self._enable_actions(False)
        self._set_text([("\n\n" + text, ("message",))])

    def _set_text(self, parts):
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        for content, tags in parts:
            self.text.insert(tk.END, _NON_BMP.sub("", content), tags)
        self.text.configure(state=tk.DISABLED)

    def _show_session(self, path: str, jump_line: int | None = None, highlight: str = ""):
        meta = store.load_meta(path)
        if path != self.current or not self.player.loaded:
            self._reset_player(meta.get("duration_s", 0))
        self.current = path
        self._enable_actions(True)

        self.title.config(text=meta.get("title", ""))
        bits = [_date(meta.get("started", ""), with_time=True)]
        if meta.get("duration_s"):
            bits.append(_minutes(meta["duration_s"]))
        bits += [meta[k] for k in ("lecturer", "room") if meta.get(k)]
        if meta.get("bookmarks"):
            n = len(meta["bookmarks"])
            bits.append(f"★ {n} bookmark{'s' if n != 1 else ''}")
        # Course on its own line; no line breaks inside a name or room
        details = "  ·  ".join(b.replace(" ", " ") for b in bits if b)
        self.meta.config(text="\n".join(x for x in (meta.get("course", ""), details) if x))

        notice, color = "", ui.FG_DIM
        status = meta.get("status", "")
        if status == store.STATUS_INCOMPLETE:
            notice, color = (f"⚠ {meta.get('failed_chunks', 0)} part(s) could not be "
                             "transcribed — More ▾ → Transcribe again"), ui.WARN
        elif status in (store.STATUS_RECORDING, store.STATUS_FINISHING):
            done = store.format_duration(meta.get("transcribed_until_s", 0))
            notice, color = f"{STATUS_MARKS[status][0]} Still transcribing — text up to {done}", ui.BOOKMARK
        if notice:
            self.notice.config(text=notice, fg=color)
            self.notice.pack(fill=tk.X, pady=(ui.px(6), 0), after=self.meta)
        else:
            self.notice.pack_forget()

        md = os.path.join(path, store.TRANSCRIPT_MD)
        try:
            with open(md, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        body_start = store._strip_front_matter(lines)

        parts = []
        line_map = {}   # md line number → text widget line
        self._line_secs = {}
        widget_line = 1
        for n in range(body_start, len(lines)):
            line = lines[n]
            if not line.strip() or store.is_header_line(line):
                continue
            line_map[n] = widget_line
            m = _TS_LINE.match(line)
            if m:
                prefix, ts, text = m.groups()
                self._line_secs[widget_line] = store.parse_ts(ts)
                ts = ts[3:] if ts.startswith("00:") else ts     # 00:12:34 → 12:34
                if prefix and "🔖" in prefix:
                    parts.append((f"★ {ts}  {text}\n", ("bookmark", "jump")))
                elif prefix:
                    parts.append((f"⚠ {ts}  {text}\n", ("warn",)))
                else:
                    parts.append((f"{ts}    ", ("ts", "jump")))
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
        self.clear_link.pack(side=tk.LEFT, padx=(ui.px(8), 0))
        self.results = [{"type": "hit", **r} for r in store.search(self.root_dir, query)]
        n = len(self.results)
        self._set_caption(f"{n} match{'es' if n != 1 else ''}")
        self.list.set_items(self.results[:RESULTS_PAGE], empty_text="Nothing found.")
        self._show_more_link()
        if self.results:
            self.list.select(0)
        else:
            self._show_message("Nothing found.")

    def _show_more_link(self):
        """Cards are slow to build in bulk — show results a page at a time."""
        shown = len(self.list.cards)
        if self.mode == "search" and shown < len(self.results):
            ui.Link(self.list.inner, f"Show more  ({len(self.results) - shown} left)",
                    self._show_more, size=10).pack(pady=ui.px(12))

    def _show_more(self):
        shown = len(self.list.cards)
        self.list.inner.winfo_children()[-1].destroy()
        self.list.add_items(self.results[shown:shown + RESULTS_PAGE])
        self._show_more_link()

    def _clear_search(self):
        if self._search_job is not None:
            self.win.after_cancel(self._search_job)
            self._search_job = None
        self.mode = "browse"
        self.clear_link.pack_forget()
        if self.search.value():
            self.search.clear()
        self._show_browse()

    # ─── Actions ────────────────────────────────────────────

    # ─── Audio player ───────────────────────────────────────

    def _reset_player(self, duration_s: float):
        """New lecture selected: stop, and show its length (the file opens on first play)."""
        self.player.close()
        self._set_now_line(None)
        self.play_btn.config(text="▶")
        self.slider.set(0)
        self.pos_label.config(text="0:00")
        self.dur_label.config(text=store.format_duration(duration_s))

    def _ensure_loaded(self) -> bool:
        if self.player.loaded:
            return True
        audio = store.find_audio(self.current) if self.current else None
        if not audio:
            self.status.config(text="No audio file in this lecture.")
            return False
        if not AudioPlayer.available:
            launcher.open_path(audio)          # no built-in player on this system
            return False
        try:
            self.player.load(audio)
        except OSError as e:
            self.status.config(text=f"Can't play this audio here ({e}) — opening it in your audio app.")
            launcher.open_path(audio)
            return False
        self.dur_label.config(text=store.format_duration(self.player.duration_ms / 1000))
        return True

    def _toggle_play(self):
        if not self._ensure_loaded():
            return
        if self.player.playing:
            self.player.pause()
        else:
            self.player.play()
        self._update_player()

    def _play_from(self, seconds: float):
        if self._ensure_loaded():
            self.player.play(int(seconds * 1000))
            self._update_player()

    def _skip(self, seconds: float):
        if self.player.loaded:
            self.player.seek(self.player.position_ms() + int(seconds * 1000))
            self._update_player()

    def _on_seek(self, fraction: float):
        if self._ensure_loaded():
            self.player.seek(int(fraction * self.player.duration_ms))
            self._update_player()

    def _cycle_speed(self):
        speed = SPEEDS[(SPEEDS.index(self.player.speed) + 1) % len(SPEEDS)] \
            if self.player.speed in SPEEDS else 1.0
        self.player.set_speed(speed)
        self.speed_link.config(text=f"{speed:g}×")

    def _on_jump_click(self, event):
        line = int(self.text.index(f"@{event.x},{event.y}").split(".")[0])
        if line in self._line_secs:
            self._play_from(self._line_secs[line])

    def _key(self, action):
        """Space / ← / → control playback, unless you're typing in a field."""
        if isinstance(self.win.focus_get(), tk.Entry):
            return None
        action()
        return "break"

    def _update_player(self):
        if not self.player.loaded:
            return
        pos = self.player.position_ms()
        self.play_btn.config(text="❚❚" if self.player.playing else "▶")
        self.pos_label.config(text=store.format_duration(pos / 1000))
        self.slider.set(pos / max(1, self.player.duration_ms))
        # Shade the paragraph that's playing
        secs = pos / 1000
        line = max((ln for ln, s in self._line_secs.items() if s <= secs), default=None,
                   key=lambda ln: self._line_secs[ln])
        self._set_now_line(line)

    def _set_now_line(self, line: int | None):
        if line == self._now_line:
            return
        self.text.tag_remove("now", "1.0", tk.END)
        if line is not None:
            self.text.tag_add("now", f"{line}.0", f"{line}.end+1c")
        self._now_line = line

    def _tick(self):
        try:
            if self.player.loaded:
                self._update_player()
        except OSError:
            pass
        self.win.after(250, self._tick)

    def _close(self):
        self.player.close()
        self.win.destroy()

    def _open_transcript(self):
        if self.current:
            launcher.open_path(os.path.join(self.current, store.TRANSCRIPT_MD))

    def _open_folder(self):
        if self.current:
            launcher.open_path(self.current)

    def _more(self):
        """Less frequent actions; “Move to course” lists the courses."""
        if not self.current:
            return
        current_course = store.load_meta(self.current).get("course", "")
        menu = ui.popup_menu(self.win)
        menu.add_command(label="Improve transcript (more accurate model)…", command=self._improve)
        menu.add_command(label="Transcribe again…", command=self._retranscribe)
        menu.add_separator()
        menu.add_command(label="Rename…", command=self._rename)
        move = ui.popup_menu(menu)
        for c in self.courses:
            if c["name"] != current_course:
                move.add_command(label=c["name"], command=lambda n=c["name"]: self._move_to(n))
        if move.index(tk.END) is not None:
            move.add_separator()
        move.add_command(label="New course…", command=self._move_to_new)
        menu.add_cascade(label="Move to course", menu=move)
        menu.add_command(label="Open folder", command=self._open_folder)
        menu.tk_popup(self.more.winfo_rootx(), self.more.winfo_rooty() + self.more.winfo_height())

    def _improve(self):
        if not self._can_transcribe(self.current):
            return
        minutes = _minutes(store.load_meta(self.current).get("duration_s", 0))
        if messagebox.askyesno(
                "Improve transcript",
                "Transcribe this lecture again with Whisper Large v3 Turbo?\n\n"
                "It is clearly more accurate, especially with technical terms, "
                f"but slower: expect roughly as long as the recording ({minutes}). "
                "It runs in the background on the power-saving cores, so you can keep "
                "using your laptop. The first time downloads the model (1.6 GB).",
                parent=self.win):
            self._start_job(self.current, BETTER_MODEL)

    def _retranscribe(self):
        if self._can_transcribe(self.current) and messagebox.askyesno(
                "Transcribe again",
                "Re-transcribe this lecture from its audio with the lecture model?\n\n"
                "The current transcript will be replaced. This runs in the background "
                "and can take a while for a long lecture.", parent=self.win):
            self._start_job(self.current)

    def _can_transcribe(self, path: str | None) -> bool:
        if not path or path in self._jobs:
            return False
        if store.is_session_busy(path):
            messagebox.showinfo("Still recording",
                                "This lecture is still being recorded or finished by the app.",
                                parent=self.win)
            return False
        if not store.find_audio(path):
            messagebox.showwarning("No audio", "This lecture has no audio file.", parent=self.win)
            return False
        return True

    def _start_job(self, path: str, model: str | None = None):
        args = ["--transcribe", path] + (["--model", model] if model else [])
        proc = launcher.spawn("lecture", *args)
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
            self.status.config(text=f"Transcribing “{meta.get('title', '')}”…  {done} / {total}")
            self.win.after(2000, lambda: self._poll_job(path))
            return
        del self._jobs[path]
        ok = proc.returncode == 0
        self.status.config(text=f"“{meta.get('title', '')}”: "
                                + ("new transcript ready." if ok else "transcription failed — see whisper.log."))
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
                self._reset_player(meta.get("duration_s", 0))   # Windows locks open files
                self.current = store.rename_session(self.current, title)
            except OSError as e:
                messagebox.showerror("Rename failed", str(e), parent=self.win)
            self.refresh()

    def _move_to_new(self):
        course = simpledialog.askstring("Move to new course", "Course name:", parent=self.win)
        if course and course.strip():
            self._move_to(course.strip())

    def _move_to(self, course: str):
        if not self.current or course == store.load_meta(self.current).get("course"):
            return
        try:
            self.player.close()                          # Windows locks open files
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
