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

import course_calendar
import lecture_store as store
import settings
import ui


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

        self.win = ui.window("Start lecture")
        self.win.configure(padx=24, pady=20)
        self.win.attributes("-topmost", True)
        self.win.columnconfigure(0, weight=1)

        ui.heading(self.win, "Start lecture").grid(row=0, column=0, sticky="w", pady=(0, 14))
        row = 1

        self.use_cal = tk.BooleanVar(value=self.event is not None)
        if self.event:
            ev = self.event
            box = tk.Frame(self.win, bg=ui.BG_ENTRY)
            box.grid(row=row, column=0, sticky="ew", pady=(0, 16))
            tk.Frame(box, bg=ui.ACCENT, width=3).pack(side=tk.LEFT, fill=tk.Y)
            body = tk.Frame(box, bg=ui.BG_ENTRY, padx=14, pady=10)
            body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            meta = "  ·  ".join(x for x in ("Now on your timetable",f"{ev.start:%H:%M}–{ev.end:%H:%M}",
                                            ev.kind) if x)
            tk.Label(body, text=meta, font=(ui.FONT, 9), fg=ui.ACCENT, bg=ui.BG_ENTRY,
                     anchor="w").pack(fill=tk.X)
            tk.Label(body, text=ev.course, font=(ui.FONT, 10, "bold"), fg=ui.FG, bg=ui.BG_ENTRY,
                     anchor="w", wraplength=400, justify=tk.LEFT).pack(fill=tk.X, pady=(2, 0))
            details = "  ·  ".join(x for x in (ev.room, ev.lecturer) if x)
            if details:
                tk.Label(body, text=details, font=(ui.FONT, 9), fg=ui.FG_DIM, bg=ui.BG_ENTRY,
                         anchor="w", wraplength=400, justify=tk.LEFT).pack(fill=tk.X)
            ui.Check(body, "Fill in from timetable", self.use_cal, command=self._apply_defaults,
                     bg=ui.BG_ENTRY).pack(anchor="w", pady=(6, 0))
            row += 1

        def label(text):
            nonlocal row
            ui.caption(self.win, text).grid(row=row, column=0, sticky="w", pady=(0, 3))
            row += 1

        label("Course")
        course_names = sorted(self.courses)
        if self.event and self.event.course not in self.courses:
            course_names.insert(0, self.event.course)
        self.course_var = tk.StringVar()
        self.course_box = ui.ComboField(self.win, self.course_var, values=course_names,
                                        on_pick=self._on_course_change, width=48)
        self.course_box.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        self.course_box.entry.bind("<FocusOut>", lambda e: self._on_course_change(), add="+")
        row += 1

        label("Title")
        self.title_var = tk.StringVar()
        title = ui.Field(self.win, self.title_var, width=48)
        title.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        self.title_entry = title.entry
        self.title_entry.bind("<Key>", lambda e: setattr(self, "_title_edited", True))
        row += 1

        label("Language")
        self.lang_var = tk.StringVar()
        ui.Segmented(self.win, list(store.LANGUAGES.values()), self.lang_var
                     ).grid(row=row, column=0, sticky="w")
        row += 1

        buttons = tk.Frame(self.win, bg=ui.BG)
        buttons.grid(row=row, column=0, sticky="e", pady=(22, 0))
        ui.Button(buttons, "●  Start recording", self._start, kind="record").pack(side=tk.RIGHT)
        ui.Link(buttons, "Cancel", self._cancel, size=10).pack(side=tk.RIGHT, padx=(0, 18))

        self.win.bind("<Return>", lambda e: self._start())
        self.win.bind("<Escape>", lambda e: self._cancel())
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

        self._title_edited = False
        self._last_course = None
        self._apply_defaults(initial=True)

        ui.center(self.win)
        self.win.after(50, self.win.focus_force)

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
            self.course_box.entry.focus_set()
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
