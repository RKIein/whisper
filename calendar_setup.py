"""
Calendar setup — import a timetable (.ics) or set an iCal subscription URL.

TraiNex: export "Studienplan" as .ics (or copy its subscription link).
Google/Outlook: use the calendar's "secret address in iCal format".
Runs as its own process (tkinter needs a main thread).
"""

import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk

import course_calendar
import settings

BG = "#f3f3f3"
FG = "#1a1a1a"
FG_DIM = "#666666"
OK = "#2e7d32"
ERR = "#c62828"


class CalendarSetup:

    def __init__(self):
        self.win = tk.Tk()
        self.win.title("Lecture calendar")
        self.win.configure(bg=BG, padx=20, pady=16)
        self.win.resizable(False, False)

        tk.Label(self.win, text="Match lectures to your timetable",
                 font=("Segoe UI", 12, "bold"), bg=BG, fg=FG).pack(anchor="w")
        tk.Label(self.win, bg=BG, fg=FG_DIM, font=("Segoe UI", 9), justify=tk.LEFT,
                 text="When you start a lecture, the event running right now fills in\n"
                      "course, type, room and lecturer automatically.").pack(anchor="w", pady=(2, 12))

        ttk.Button(self.win, text="Import .ics file…", command=self._import).pack(anchor="w")

        tk.Label(self.win, text="…or subscription URL (refreshed every 6 h, works offline from cache):",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=(12, 2))
        row = tk.Frame(self.win, bg=BG)
        row.pack(fill=tk.X)
        self.url_var = tk.StringVar(value=settings.get("calendar_url", ""))
        ttk.Entry(row, textvariable=self.url_var, width=56).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Save", command=self._save_url).pack(side=tk.LEFT, padx=(6, 0))

        self.status = tk.Label(self.win, bg=BG, font=("Segoe UI", 9), justify=tk.LEFT, anchor="w")
        self.status.pack(fill=tk.X, pady=(12, 0))

        tk.Label(self.win, text="Today", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(10, 0))
        self.today = tk.Label(self.win, bg=BG, fg=FG, font=("Segoe UI", 9),
                              justify=tk.LEFT, anchor="w")
        self.today.pack(fill=tk.X)

        bottom = tk.Frame(self.win, bg=BG)
        bottom.pack(fill=tk.X, pady=(14, 0))
        ttk.Button(bottom, text="Remove calendar", command=self._remove).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Close", command=self.win.destroy).pack(side=tk.RIGHT)
        self.win.bind("<Escape>", lambda e: self.win.destroy())

        self._refresh_status()

    def _set_status(self, text: str, color: str = FG_DIM):
        self.status.config(text=text, fg=color)

    def _refresh_status(self):
        cal = course_calendar.CourseCalendar(url=settings.get("calendar_url", ""))
        events = cal.events() if cal.available else []
        if not events:
            self._set_status("No calendar set up yet.")
            self.today.config(text="—")
            return
        first, last = events[0].start, events[-1].end
        self._set_status(f"✓ {len(events)} events  ({first:%d.%m.%Y} – {last:%d.%m.%Y})", OK)
        todays = course_calendar.events_on(events, datetime.now())
        lines = [f"{e.start:%H:%M}–{e.end:%H:%M}  {e.short_label}"
                 + (f"  ·  {e.lecturer}" if e.lecturer else "") for e in todays]
        self.today.config(text="\n".join(lines) or "No events today.")

    def _import(self):
        path = filedialog.askopenfilename(
            parent=self.win, title="Choose timetable",
            filetypes=[("iCalendar", "*.ics"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            n = course_calendar.import_file(path)
            settings.put("calendar_url", "")
            self.url_var.set("")
            self._set_status(f"✓ Imported {n} events.", OK)
            self._refresh_status()
        except Exception as e:
            self._set_status(f"Import failed: {e}", ERR)

    def _save_url(self):
        url = self.url_var.get().strip()
        settings.put("calendar_url", url)
        if not url:
            self._refresh_status()
            return
        self._set_status("Downloading…")

        def _fetch():
            try:
                n = course_calendar.refresh_from_url(url)
                self.win.after(0, lambda: (self._set_status(f"✓ Downloaded {n} events.", OK),
                                           self._refresh_status()))
            except Exception as e:
                msg = f"Download failed: {e}"
                self.win.after(0, lambda: self._set_status(msg, ERR))

        threading.Thread(target=_fetch, daemon=True).start()

    def _remove(self):
        import os
        settings.put("calendar_url", "")
        self.url_var.set("")
        try:
            os.remove(course_calendar.CALENDAR_FILE)
        except OSError:
            pass
        self._refresh_status()

    def run(self):
        self.win.mainloop()


def main(argv: list[str]) -> int:
    CalendarSetup().run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
