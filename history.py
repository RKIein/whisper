"""
History Panel — floating overlay near the system tray.

A clean, frameless tkinter window that shows transcription history.
Appears above the taskbar, disappears when it loses focus.
Each entry has a copy button. Includes a search bar at the top.
"""

import logging
import os
import re
import threading
import tkinter as tk

import config

logger = logging.getLogger(__name__)

_LOG_PATTERN = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (?:\[\w+\] )?\((\d+\.\d+)s\) (.+)"
)

# ─── Colors (light, matches Windows light taskbar) ──────────

BG = "#f3f3f3"
BG_ENTRY = "#ffffff"
BG_ENTRY_HOVER = "#e8f0fe"
FG = "#1a1a1a"
FG_DIM = "#888888"
FG_SEARCH = "#aaaaaa"
FG_TIME = "#4a80c4"
FG_DURATION = "#999999"
ACCENT = "#4a80c4"
COPY_FG = "#999999"
COPY_SUCCESS = "#2e9e41"
SCROLL_THUMB = "#c4c4c4"
SCROLL_THUMB_HOVER = "#a0a0a0"
SCROLL_THUMB_DRAG = "#888888"
SCROLL_WIDTH = 14       # hit area width (invisible, generous for grabbing)
SCROLL_PILL_W = 5       # visible pill width
SCROLL_PILL_PAD = 4     # top/bottom padding
SCROLL_PILL_RADIUS = 3  # rounded corner radius

PANEL_WIDTH = 440
PANEL_HEIGHT = 500


def _parse_log() -> list[dict]:
    """Parse the transcription log file into structured entries."""
    log_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), config.LOG_FILE
    )
    entries = []
    if not os.path.exists(log_path):
        return entries

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                m = _LOG_PATTERN.match(line.strip())
                if m:
                    dur = float(m.group(2))
                    if dur < 60:
                        dur_str = f"{dur:.0f}s"
                    else:
                        mins = int(dur) // 60
                        secs = int(dur) % 60
                        dur_str = f"{mins}m {secs}s"

                    parts = m.group(1).split(" ")
                    entries.append({
                        "date": parts[0],
                        "time": parts[1] if len(parts) > 1 else m.group(1),
                        "duration": dur_str,
                        "text": m.group(3),
                    })
    except Exception as e:
        logger.error(f"Failed to read history: {e}")

    return list(reversed(entries))  # Newest first


class PillScrollbar(tk.Canvas):
    """Modern pill-shaped scrollbar drawn on a canvas — no trough box."""

    def __init__(self, parent, command=None, **kw):
        kw.setdefault("width", SCROLL_WIDTH)
        kw.setdefault("bg", BG)
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        super().__init__(parent, **kw)

        self._command = command
        self._thumb_id = None
        self._dragging = False
        self._drag_start_y = 0
        self._drag_start_top = 0.0
        self._top = 0.0
        self._bottom = 1.0
        self._hover = False

        self.bind("<Configure>", self._draw)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def set(self, top, bottom):
        """Called by the scrollable widget to update thumb position."""
        self._top = float(top)
        self._bottom = float(bottom)
        self._draw()

    def _draw(self, event=None):
        self.delete("thumb")
        h = self.winfo_height()
        if h < 1:
            return

        # Don't draw if content fits
        if self._top <= 0 and self._bottom >= 1:
            return

        track_h = h - SCROLL_PILL_PAD * 2
        y0 = SCROLL_PILL_PAD + self._top * track_h
        y1 = SCROLL_PILL_PAD + self._bottom * track_h

        # Minimum thumb height
        min_h = SCROLL_PILL_RADIUS * 6
        if y1 - y0 < min_h:
            mid = (y0 + y1) / 2
            y0 = mid - min_h / 2
            y1 = mid + min_h / 2

        # Center the pill horizontally
        cx = self.winfo_width() / 2
        x0 = cx - SCROLL_PILL_W / 2
        x1 = cx + SCROLL_PILL_W / 2

        color = SCROLL_THUMB_DRAG if self._dragging else (
            SCROLL_THUMB_HOVER if self._hover else SCROLL_THUMB
        )

        # Draw rounded rectangle (pill shape)
        r = SCROLL_PILL_RADIUS
        self._thumb_id = self.create_polygon(
            x0 + r, y0,
            x1 - r, y0,
            x1, y0,
            x1, y0 + r,
            x1, y1 - r,
            x1, y1,
            x1 - r, y1,
            x0 + r, y1,
            x0, y1,
            x0, y1 - r,
            x0, y0 + r,
            x0, y0,
            fill=color, outline=color,
            smooth=True, tags="thumb",
        )

    def _on_press(self, event):
        h = self.winfo_height() - SCROLL_PILL_PAD * 2
        if h < 1:
            return
        frac = (event.y - SCROLL_PILL_PAD) / h

        # If click is on the thumb, start drag
        if self._top <= frac <= self._bottom:
            self._dragging = True
            self._drag_start_y = event.y
            self._drag_start_top = self._top
            self._draw()
        else:
            # Jump to click position
            thumb_size = self._bottom - self._top
            new_top = max(0, min(frac - thumb_size / 2, 1 - thumb_size))
            if self._command:
                self._command("moveto", str(new_top))

    def _on_drag(self, event):
        if not self._dragging:
            return
        h = self.winfo_height() - SCROLL_PILL_PAD * 2
        if h < 1:
            return
        dy = (event.y - self._drag_start_y) / h
        thumb_size = self._bottom - self._top
        new_top = max(0, min(self._drag_start_top + dy, 1 - thumb_size))
        if self._command:
            self._command("moveto", str(new_top))

    def _on_release(self, event):
        self._dragging = False
        self._draw()

    def _on_enter(self, event):
        self._hover = True
        self._draw()

    def _on_leave(self, event):
        self._hover = False
        if not self._dragging:
            self._draw()


class HistoryPanel:
    """Floating overlay panel for transcription history."""

    def __init__(self):
        self._root = None
        self._entries = []
        self._entry_widgets = []
        self._thread = None

    def show(self):
        """Show the panel. If already open, bring to front."""
        if self._root is not None:
            try:
                self._root.lift()
                self._root.focus_force()
                return
            except tk.TclError:
                self._root = None

        self._thread = threading.Thread(target=self._create_window, daemon=True)
        self._thread.start()

    def _create_window(self):
        self._entries = _parse_log()

        root = tk.Tk()
        self._root = root
        root.title("Whisper History")
        root.configure(bg=BG)
        root.overrideredirect(True)

        # Position above taskbar, right side
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = screen_w - PANEL_WIDTH - 12
        y = screen_h - PANEL_HEIGHT - 52
        root.geometry(f"{PANEL_WIDTH}x{PANEL_HEIGHT}+{x}+{y}")

        root.attributes("-topmost", True)

        root.bind("<FocusOut>", self._on_focus_out)
        root.bind("<Escape>", lambda e: self._close())

        # ─── Search (seamless, no box) ──────────────────────
        search_bar = tk.Frame(root, bg=BG, padx=16, pady=12)
        search_bar.pack(fill=tk.X)

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._filter_entries())

        search_entry = tk.Entry(
            search_bar,
            textvariable=self._search_var,
            font=("Segoe UI", 12),
            bg=BG, fg=FG,
            insertbackground=FG,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
        )
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=2)
        self._search_entry = search_entry
        self._show_placeholder()

        search_entry.bind("<FocusIn>", lambda e: self._on_search_focus(e, True))
        search_entry.bind("<FocusOut>", lambda e: self._on_search_focus(e, False))

        # Close button
        close_btn = tk.Label(
            search_bar, text="✕", font=("Segoe UI", 10),
            fg=FG_DIM, bg=BG, cursor="hand2",
        )
        close_btn.pack(side=tk.RIGHT, padx=(8, 0))
        close_btn.bind("<Button-1>", lambda e: self._close())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg="#333333"))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=FG_DIM))

        # ─── Scrollable entries ─────────────────────────────
        container = tk.Frame(root, bg=BG)
        container.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0, bd=0)
        scrollbar = PillScrollbar(container, command=canvas.yview)

        self._scroll_frame = tk.Frame(canvas, bg=BG)

        self._scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )

        canvas.create_window(
            (0, 0), window=self._scroll_frame, anchor=tk.NW,
            width=PANEL_WIDTH - SCROLL_WIDTH - 4,
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse wheel
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        self._canvas = canvas

        self._populate_entries()

        root.after(50, lambda: root.focus_force())
        root.mainloop()
        self._root = None

    def _show_placeholder(self):
        if not self._search_var.get():
            self._search_entry.config(fg=FG_SEARCH)
            self._search_entry.delete(0, tk.END)
            self._search_entry.insert(0, "Search")
            self._search_entry._placeholder = True

    def _on_search_focus(self, event, focused):
        if focused and getattr(self._search_entry, "_placeholder", False):
            self._search_entry.delete(0, tk.END)
            self._search_entry.config(fg=FG)
            self._search_entry._placeholder = False
        elif not focused and not self._search_var.get():
            self._show_placeholder()

    def _populate_entries(self):
        self._entry_widgets.clear()

        if not self._entries:
            tk.Label(
                self._scroll_frame,
                text="No transcriptions yet",
                font=("Segoe UI", 11),
                fg=FG_DIM, bg=BG,
                justify=tk.CENTER, pady=60,
            ).pack(fill=tk.X)
            return

        for i, entry in enumerate(self._entries):
            frame = self._create_entry_widget(i, entry)
            self._entry_widgets.append((frame, entry))

    def _create_entry_widget(self, index, entry):
        frame = tk.Frame(
            self._scroll_frame, bg=BG_ENTRY,
            padx=14, pady=9,
            highlightthickness=0,
        )
        frame.pack(fill=tk.X, padx=8, pady=2)

        # Hover
        def _set_bg(widget, color):
            try:
                widget.config(bg=color)
            except tk.TclError:
                pass
            for child in widget.winfo_children():
                _set_bg(child, color)

        frame.bind("<Enter>", lambda e: _set_bg(frame, BG_ENTRY_HOVER))
        frame.bind("<Leave>", lambda e: _set_bg(frame, BG_ENTRY))

        # Metadata row
        meta = tk.Frame(frame, bg=BG_ENTRY)
        meta.pack(fill=tk.X, pady=(0, 2))

        meta_text = f"{entry['time']}  ·  {entry['date']}  ·  {entry['duration']}"
        tk.Label(
            meta, text=meta_text,
            font=("Segoe UI", 9), fg=FG_DIM, bg=BG_ENTRY,
        ).pack(side=tk.LEFT)

        # Copy button
        copy_btn = tk.Label(
            meta, text="Copy",
            font=("Segoe UI", 9), fg=COPY_FG, bg=BG_ENTRY,
            cursor="hand2",
        )
        copy_btn.pack(side=tk.RIGHT)
        copy_btn.bind("<Button-1>", lambda e, t=entry["text"], b=copy_btn: self._copy(t, b))
        copy_btn.bind("<Enter>", lambda e: copy_btn.config(fg="#555555"))
        copy_btn.bind("<Leave>", lambda e: copy_btn.config(fg=COPY_FG))

        # Text
        tk.Label(
            frame, text=entry["text"],
            font=("Segoe UI", 10), fg=FG, bg=BG_ENTRY,
            wraplength=PANEL_WIDTH - 52,
            justify=tk.LEFT, anchor=tk.W,
        ).pack(fill=tk.X)

        return frame

    def _copy(self, text, button):
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
            button.config(text="Copied!", fg=COPY_SUCCESS)
            self._root.after(1500, lambda: button.config(text="Copy", fg=COPY_FG))
        except Exception:
            pass

    def _filter_entries(self):
        query = self._search_var.get().lower()
        if getattr(self._search_entry, "_placeholder", False):
            query = ""

        for frame, entry in self._entry_widgets:
            matches = not query or query in entry["text"].lower() or query in entry["date"] or query in entry["time"]
            if matches:
                frame.pack(fill=tk.X, padx=8, pady=2)
            else:
                frame.pack_forget()

    def _on_focus_out(self, event):
        if self._root is None:
            return
        self._root.after(100, self._check_focus)

    def _check_focus(self):
        if self._root is None:
            return
        try:
            focused = self._root.focus_get()
            if focused is None:
                self._close()
        except (tk.TclError, KeyError):
            self._close()

    def _close(self):
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None


def open_history():
    """Launch the history panel as a separate process (tkinter needs its own main thread)."""
    import subprocess
    import sys
    script = os.path.abspath(__file__)
    python = sys.executable
    try:
        subprocess.Popen(
            [python, script],
            cwd=os.path.dirname(script),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logger.info("History panel launched")
    except Exception as e:
        logger.error(f"Failed to launch history panel: {e}")


if __name__ == "__main__":
    panel = HistoryPanel()
    panel._create_window()
