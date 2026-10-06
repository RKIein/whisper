"""
Calendar setup — import a timetable (.ics) or set an iCal subscription URL.

TraiNex: export "Studienplan" as .ics (or copy its subscription link).
Google/Outlook: use the calendar's "secret address in iCal format".
Runs as its own process (tkinter needs a main thread).
"""

import os
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox

import course_calendar
import settings
import ui

WIDTH = 460


class CalendarSetup:

    def __init__(self):
        self.win = ui.window("Timetable")
        self.win.configure(padx=24, pady=20)

        ui.heading(self.win, "Timetable").pack(anchor="w")
        tk.Label(self.win, bg=ui.BG, fg=ui.FG_DIM, font=(ui.FONT, 9), justify=tk.LEFT,
                 anchor="w", wraplength=WIDTH - 48,
                 text="When you start a lecture, the event on right now fills in "
                      "course, type, room and lecturer.").pack(fill=tk.X, pady=(2, 14))

        # ─── Status card ────────────────────────────────────
        card = tk.Frame(self.win, bg=ui.BG_ENTRY, padx=14, pady=12)
        card.pack(fill=tk.X)
        self.status = tk.Label(card, font=(ui.FONT, 11, "bold"), bg=ui.BG_ENTRY, anchor="w")
        self.status.pack(fill=tk.X)
        self.range = tk.Label(card, font=(ui.FONT, 9), fg=ui.FG_DIM, bg=ui.BG_ENTRY,
                              anchor="w", justify=tk.LEFT, wraplength=WIDTH - 76)
        self.range.pack(fill=tk.X)
        self.actions = tk.Frame(card, bg=ui.BG_ENTRY)
        self.actions.pack(fill=tk.X, pady=(10, 0))

        # ─── Upcoming events ────────────────────────────────
        self.day_caption = ui.caption(self.win, "Today")
        self.day_caption.pack(fill=tk.X, pady=(16, 4))
        self.day_list = tk.Frame(self.win, bg=ui.BG)
        self.day_list.pack(fill=tk.X)

        # ─── Subscription link ──────────────────────────────
        self.url_caption = ui.caption(
            self.win, "Or subscribe by link (refreshed every 6 h, works offline)")
        self.url_caption.pack(fill=tk.X, pady=(18, 4))
        row = tk.Frame(self.win, bg=ui.BG)
        row.pack(fill=tk.X)
        self.url_var = tk.StringVar(value=settings.get("calendar_url", ""))
        url = ui.Field(row, self.url_var, width=40, size=9)
        url.pack(side=tk.LEFT, fill=tk.X, expand=True)
        url.entry.bind("<Return>", lambda e: self._save_url())
        ui.Link(row, "Save link", self._save_url, size=10).pack(side=tk.LEFT, padx=(12, 0))

        bottom = tk.Frame(self.win, bg=ui.BG)
        bottom.pack(fill=tk.X, pady=(20, 0))
        ui.Button(bottom, "Done", self.win.destroy, kind="plain").pack(side=tk.RIGHT)
        self.win.bind("<Escape>", lambda e: self.win.destroy())

        self._refresh_status()
        ui.center(self.win)

    def _set_status(self, text: str, color: str = ui.FG_DIM, detail: str = ""):
        self.status.config(text=text, fg=color)
        self.range.config(text=detail)

    def _set_actions(self, has_calendar: bool):
        for child in self.actions.winfo_children():
            child.destroy()
        if has_calendar:
            ui.Link(self.actions, "Import another file…", self._import, bg=ui.BG_ENTRY,
                    size=10).pack(side=tk.LEFT)
            ui.Link(self.actions, "Remove", self._remove, bg=ui.BG_ENTRY,
                    size=10).pack(side=tk.LEFT, padx=(18, 0))
        else:
            ui.Button(self.actions, "Import .ics file…", self._import).pack(side=tk.LEFT)

    def _refresh_status(self):
        cal = course_calendar.CourseCalendar(url=settings.get("calendar_url", ""))
        events = cal.events() if cal.available else []
        self._set_actions(bool(events))
        if not events:
            self._set_status("No timetable yet", ui.FG,
                             "TraiNex: Studienplan → export as .ics, then import it here.")
            self._show_day(None, "")
            return
        first, last = events[0].start, events[-1].end
        source = "subscription link" if settings.get("calendar_url", "") else "imported file"
        self._set_status(f"✓  {len(events)} events", ui.OK,
                         f"{first:%d.%m.%Y} – {last:%d.%m.%Y}  ·  {source}")

        today = datetime.now()
        todays = course_calendar.events_on(events, today)
        if todays:
            self._show_day(todays, "Today")
            return
        upcoming = next((e for e in events if e.start > today), None)
        if upcoming is None:
            self._show_day([], "Today")
            return
        day = upcoming.start
        label = "Tomorrow" if day.date() == (today + timedelta(days=1)).date() else f"Next: {day:%A %d.%m.}"
        self._show_day(course_calendar.events_on(events, day), label)

    def _show_day(self, events, label: str):
        """events=None hides the section (no timetable yet)."""
        for child in self.day_list.winfo_children():
            child.destroy()
        if events is None:
            self.day_caption.pack_forget()
            self.day_list.pack_forget()
            return
        self.day_caption.config(text=label)
        self.day_caption.pack(fill=tk.X, pady=(16, 4), before=self.url_caption)
        self.day_list.pack(fill=tk.X, before=self.url_caption)
        if not events:
            tk.Label(self.day_list, text="No events.", font=(ui.FONT, 10), fg=ui.FG_DIM,
                     bg=ui.BG, anchor="w").pack(fill=tk.X)
            return
        for e in events:
            card = tk.Frame(self.day_list, bg=ui.BG_ENTRY, padx=14, pady=8)
            card.pack(fill=tk.X, pady=(0, 2))
            meta = "  ·  ".join(x for x in (f"{e.start:%H:%M}–{e.end:%H:%M}", e.kind, e.room) if x)
            tk.Label(card, text=meta, font=(ui.FONT, 9), fg=ui.FG_DIM, bg=ui.BG_ENTRY,
                     anchor="w").pack(fill=tk.X)
            tk.Label(card, text=e.course or e.summary, font=(ui.FONT, 10), fg=ui.FG,
                     bg=ui.BG_ENTRY, anchor="w", justify=tk.LEFT,
                     wraplength=WIDTH - 76).pack(fill=tk.X)
            if e.lecturer:
                tk.Label(card, text=e.lecturer, font=(ui.FONT, 9), fg=ui.FG_DIM, bg=ui.BG_ENTRY,
                         anchor="w").pack(fill=tk.X)

    def _import(self):
        path = filedialog.askopenfilename(
            parent=self.win, title="Choose timetable",
            initialdir=os.path.join(os.path.expanduser("~"), "Downloads"),
            filetypes=[("iCalendar", "*.ics"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            course_calendar.import_file(path)
            settings.put("calendar_url", "")
            self.url_var.set("")
            self._refresh_status()
        except Exception as e:
            self._set_status("Import failed", ui.WARN, str(e))

    def _save_url(self):
        url = self.url_var.get().strip()
        settings.put("calendar_url", url)
        if not url:
            self._refresh_status()
            return
        self._set_status("Downloading…")

        def _fetch():
            try:
                course_calendar.refresh_from_url(url)
                self.win.after(0, self._refresh_status)
            except Exception as e:
                msg = str(e)
                self.win.after(0, lambda: self._set_status("Download failed", ui.WARN, msg))

        threading.Thread(target=_fetch, daemon=True).start()

    def _remove(self):
        if not messagebox.askyesno("Remove timetable",
                                   "Remove the timetable from the app?\n\n"
                                   "Your .ics file and your lectures are not touched.",
                                   parent=self.win):
            return
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
