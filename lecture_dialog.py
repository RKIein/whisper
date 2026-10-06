"""
Start-lecture dialog — choose course, title and language.

If a timetable is set up (Calendar ▸ in the tray menu), the event running
right now pre-fills everything: course, type (Vorlesung/Seminar), room and
lecturer. Runs as its own process (tkinter needs a main thread) and writes
the result as JSON to --out; exit code 0 = start, 1 = cancelled.
"""

import argparse
import json
import sys
import tkinter as tk
from tkinter import ttk

import course_calendar
import lecture_store as store
import settings

BG = "#f3f3f3"
FG = "#1a1a1a"
FG_DIM = "#666666"
ACCENT = "#4a80c4"
CAL_BG = "#e8f0fe"


def _lang_label(code: str) -> str:
    return store.LANGUAGES.get(code, store.LANGUAGES["auto"])


def _lang_code(label: str) -> str:
    for code, text in store.LANGUAGES.items():
        if text == label:
            return code
    return "auto"


class LectureDialog:

    def __init__(self, out_path: str):
        self.out_path = out_path
        self.result = None
        s = settings.load()
        self.root_dir = s["lecture_root"]
        self.courses = {c["name"]: c for c in store.list_courses(self.root_dir)}

        cal = course_calendar.CourseCalendar(url=s.get("calendar_url", ""))
        self.event = cal.current() if cal.available else None

        self.win = tk.Tk()
        self.win.title("Start lecture")
        self.win.configure(bg=BG, padx=20, pady=16)
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)

        row = 0
        self.use_cal = tk.BooleanVar(value=self.event is not None)
        if self.event:
            ev = self.event
            box = tk.Frame(self.win, bg=CAL_BG, padx=12, pady=8)
            box.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 12))
            tk.Label(box, text=f"On your timetable now  ·  {ev.start:%H:%M}–{ev.end:%H:%M}  ·  {ev.kind}",
                     font=("Segoe UI", 10, "bold"), bg=CAL_BG, fg=FG,
                     anchor="w").pack(fill=tk.X)
            tk.Label(box, text=ev.course, font=("Segoe UI", 10), bg=CAL_BG, fg=FG,
                     anchor="w", wraplength=420, justify=tk.LEFT).pack(fill=tk.X)
            details = "  ·  ".join(x for x in (ev.room, ev.lecturer) if x)
            if details:
                tk.Label(box, text=details, font=("Segoe UI", 9), bg=CAL_BG, fg=FG_DIM,
                         anchor="w").pack(fill=tk.X)
            tk.Checkbutton(box, text="Use calendar info", variable=self.use_cal,
                           bg=CAL_BG, activebackground=CAL_BG,
                           command=self._apply_defaults).pack(anchor="w", pady=(4, 0))
            row += 1

        def label(text, r):
            tk.Label(self.win, text=text, bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 9)).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 12))

        label("Course", row)
        course_names = sorted(self.courses)
        if self.event and self.event.course not in self.courses:
            course_names.insert(0, self.event.course)
        self.course_var = tk.StringVar()
        self.course_box = ttk.Combobox(self.win, textvariable=self.course_var,
                                       values=course_names, width=52)
        self.course_box.grid(row=row, column=1, sticky="ew", pady=4)
        self.course_box.bind("<<ComboboxSelected>>", lambda e: self._on_course_change())
        self.course_box.bind("<FocusOut>", lambda e: self._on_course_change())
        row += 1

        label("Title", row)
        self.title_var = tk.StringVar()
        self.title_entry = ttk.Entry(self.win, textvariable=self.title_var, width=54)
        self.title_entry.grid(row=row, column=1, sticky="ew", pady=4)
        self.title_entry.bind("<Key>", lambda e: setattr(self, "_title_edited", True))
        row += 1

        label("Language", row)
        self.lang_var = tk.StringVar()
        ttk.Combobox(self.win, textvariable=self.lang_var, state="readonly",
                     values=list(store.LANGUAGES.values()), width=20
                     ).grid(row=row, column=1, sticky="w", pady=4)
        row += 1

        buttons = tk.Frame(self.win, bg=BG)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side=tk.RIGHT)
        start = ttk.Button(buttons, text="● Start recording", command=self._start)
        start.pack(side=tk.RIGHT, padx=(0, 8))

        self.win.bind("<Return>", lambda e: self._start())
        self.win.bind("<Escape>", lambda e: self._cancel())
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

        self._title_edited = False
        self._last_course = None
        self._apply_defaults(initial=True)

        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        x = (self.win.winfo_screenwidth() - w) // 2
        y = (self.win.winfo_screenheight() - h) // 3
        self.win.geometry(f"+{x}+{y}")
        self.win.after(50, lambda: (self.win.focus_force(), start.focus_set()))

    # ─── Defaults ───────────────────────────────────────────

    def _apply_defaults(self, initial: bool = False):
        if self.event and self.use_cal.get():
            course = self.event.course
        elif initial or (self.event and self.course_var.get() == self.event.course):
            course = settings.get("last_course", "") or (sorted(self.courses)[0] if self.courses else "")
        else:
            course = self.course_var.get()
        self.course_var.set(course)
        self._title_edited = False
        self._on_course_change(force=True)

    def _on_course_change(self, force: bool = False):
        course = self.course_var.get().strip()
        if course == self._last_course and not force:
            return
        self._last_course = course
        info = self.courses.get(course) or {}
        language = info.get("language", "auto")
        self.lang_var.set(_lang_label(language))
        if not self._title_edited:
            kind = self.event.kind if (self.event and self.use_cal.get()
                                       and course == self.event.course) else ""
            path = store.course_dir(self.root_dir, course) if course else ""
            self.title_var.set(store.next_title(path, language, kind) if course else
                               store.default_title_base(language))

    # ─── Result ─────────────────────────────────────────────

    def _start(self):
        course = self.course_var.get().strip()
        if not course:
            self.course_box.focus_set()
            return
        use_event = self.event is not None and self.use_cal.get() and course == self.event.course
        self.result = {
            "course": course,
            "title": self.title_var.get().strip() or "Session",
            "language": _lang_code(self.lang_var.get()),
            "calendar": self.event.to_meta() if use_event else None,
        }
        with open(self.out_path, "w", encoding="utf-8") as f:
            json.dump(self.result, f, ensure_ascii=False)
        self.win.destroy()

    def _cancel(self):
        self.result = None
        self.win.destroy()

    def run(self) -> int:
        self.win.mainloop()
        return 0 if self.result else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    return LectureDialog(args.out).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
