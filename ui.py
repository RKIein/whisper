"""
Shared look for the lecture windows — same palette and widgets as the
transcription history panel: light grey background, white cards, flat
inputs, grey text links and the pill scrollbar.
"""

import os
import sys
import tkinter as tk

from history import (  # noqa: F401  (re-exported for the windows)
    ACCENT, BG, BG_ENTRY, BG_ENTRY_HOVER, COPY_FG, COPY_SUCCESS, FG, FG_DIM,
    FG_SEARCH, PillScrollbar,
)

FONT = "Segoe UI"

BG_SELECTED = "#d6e4fa"     # selected card — a step stronger than hover
BORDER = "#e2e2e2"
LINK_HOVER = "#333333"
LINK_DISABLED = "#cccccc"
RECORD = "#d9443a"          # same red as the tray icon while recording
RECORD_HOVER = "#c23a31"
ACCENT_HOVER = "#3d6fae"
OK = COPY_SUCCESS
WARN = "#c62828"
BOOKMARK = "#c77700"
HIGHLIGHT = "#fff3a3"

_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.ico")

SCALE = 1.0     # display scaling (1.25 at 125 %), set when the first window opens


def _dpi_aware():
    """
    Draw at the screen's real resolution. Without this, Windows renders the
    window at 100 % and stretches the bitmap, which makes text blurry on
    laptops set to 125 % or 150 %.
    """
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def px(n: float) -> int:
    """Pixels at 100 % → pixels on this screen (fonts scale by themselves)."""
    return int(round(n * SCALE))


def window(title: str, resizable: bool = False) -> tk.Tk:
    global SCALE
    _dpi_aware()
    win = tk.Tk()
    SCALE = max(1.0, win.winfo_fpixels("1i") / 96)
    win.title(title)
    win.configure(bg=BG)
    win.resizable(resizable, resizable)
    if os.path.exists(_ICON):
        try:
            win.iconbitmap(_ICON)
        except tk.TclError:
            pass
    return win


def center(win: tk.Misc, y_ratio: float = 3):
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    x = (win.winfo_screenwidth() - w) // 2
    y = int((win.winfo_screenheight() - h) // y_ratio)
    win.geometry(f"+{x}+{y}")


def heading(parent, text: str, bg: str = BG) -> tk.Label:
    return tk.Label(parent, text=text, font=(FONT, 13, "bold"), fg=FG, bg=bg, anchor="w")


def caption(parent, text: str, bg: str = BG) -> tk.Label:
    return tk.Label(parent, text=text, font=(FONT, 9), fg=FG_DIM, bg=bg, anchor="w")


def set_bg(widget, color: str):
    """Recolour a frame and everything in it (cards, hover)."""
    try:
        widget.config(bg=color)
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        set_bg(child, color)


# ─── Clickable text ──────────────────────────────────────────

class Link(tk.Label):
    """Grey text action, darkens on hover — like “Copy” in the history panel."""

    def __init__(self, parent, text: str, command, bg: str = BG, size: int = 9,
                 fg: str = COPY_FG, hover: str = LINK_HOVER):
        super().__init__(parent, text=text, font=(FONT, size), fg=fg, bg=bg, cursor="hand2")
        self._command, self._fg, self._hover = command, fg, hover
        self._enabled = True
        self.bind("<Button-1>", lambda e: self._enabled and self._command())
        self.bind("<Enter>", lambda e: self._enabled and self.config(fg=self._hover))
        self.bind("<Leave>", lambda e: self._enabled and self.config(fg=self._fg))

    def enable(self, on: bool = True):
        self._enabled = on
        self.config(fg=self._fg if on else LINK_DISABLED, cursor="hand2" if on else "")


class Button(tk.Label):
    """Flat filled button. kind: 'primary' (blue), 'record' (red) or 'plain' (white)."""

    _COLORS = {
        "primary": (ACCENT, ACCENT_HOVER, "#ffffff"),
        "record": (RECORD, RECORD_HOVER, "#ffffff"),
        "plain": (BG_ENTRY, BG_ENTRY_HOVER, FG),
    }

    def __init__(self, parent, text: str, command, kind: str = "primary"):
        bg, hover, fg = self._COLORS[kind]
        super().__init__(parent, text=text, font=(FONT, 10), fg=fg, bg=bg,
                         padx=px(16), pady=px(6), cursor="hand2")
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self.config(bg=hover))
        self.bind("<Leave>", lambda e: self.config(bg=bg))


class Check(tk.Label):
    """Text checkbox: ☑ / ☐ followed by a label."""

    def __init__(self, parent, text: str, variable: tk.BooleanVar, command=None, bg: str = BG):
        super().__init__(parent, font=(FONT, 9), fg=FG_DIM, bg=bg, cursor="hand2")
        self._text, self._var, self._command = text, variable, command
        self.bind("<Button-1>", lambda e: self._toggle())
        self.bind("<Enter>", lambda e: self.config(fg=LINK_HOVER))
        self.bind("<Leave>", lambda e: self.config(fg=FG_DIM))
        self._draw()

    def _toggle(self):
        self._var.set(not self._var.get())
        self._draw()
        if self._command:
            self._command()

    def _draw(self):
        self.config(text=("☑  " if self._var.get() else "☐  ") + self._text)


# ─── Inputs ──────────────────────────────────────────────────

class Field(tk.Frame):
    """White input box with a thin border that turns blue while typing."""

    def __init__(self, parent, textvariable: tk.StringVar, width: int = 40, size: int = 10):
        super().__init__(parent, bg=BG_ENTRY, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=BORDER)
        self.entry = tk.Entry(self, textvariable=textvariable, font=(FONT, size), width=width,
                              bg=BG_ENTRY, fg=FG, insertbackground=FG, relief=tk.FLAT,
                              bd=0, highlightthickness=0)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=px(10), pady=px(6))
        self.entry.bind("<FocusIn>", lambda e: self.config(highlightbackground=ACCENT), add="+")
        self.entry.bind("<FocusOut>", lambda e: self.config(highlightbackground=BORDER), add="+")


class ComboField(Field):
    """Field you can type in, with a ▾ that lists existing values."""

    def __init__(self, parent, textvariable: tk.StringVar, values=(), on_pick=None, width: int = 40):
        super().__init__(parent, textvariable, width=width)
        self._var, self._on_pick = textvariable, on_pick
        self.values = list(values)
        arrow = tk.Label(self, text="▾", font=(FONT, 11), fg=FG_DIM, bg=BG_ENTRY,
                         padx=px(10), cursor="hand2")
        arrow.pack(side=tk.RIGHT, fill=tk.Y)
        arrow.bind("<Button-1>", lambda e: self._popup())
        arrow.bind("<Enter>", lambda e: arrow.config(fg=LINK_HOVER))
        arrow.bind("<Leave>", lambda e: arrow.config(fg=FG_DIM))
        self.entry.bind("<Alt-Down>", lambda e: self._popup())

    def _popup(self):
        if not self.values:
            return
        menu = popup_menu(self)
        for v in self.values:
            menu.add_command(label=v, command=lambda v=v: self._pick(v))
        menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())

    def _pick(self, value: str):
        self._var.set(value)
        self.entry.icursor(tk.END)
        if self._on_pick:
            self._on_pick()


def popup_menu(parent) -> tk.Menu:
    return tk.Menu(parent, tearoff=0, font=(FONT, 10), bg=BG_ENTRY, fg=FG,
                   activebackground=BG_ENTRY_HOVER, activeforeground=FG, relief=tk.FLAT, bd=0)


class Segmented(tk.Frame):
    """Row of choices, one selected — bound to a StringVar holding the label."""

    def __init__(self, parent, labels, variable: tk.StringVar):
        super().__init__(parent, bg=BG_ENTRY, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=BORDER)
        self._var = variable
        self._items = {}
        for text in labels:
            item = tk.Label(self, text=text, font=(FONT, 10), padx=px(14), pady=px(5), cursor="hand2")
            item.pack(side=tk.LEFT)
            item.bind("<Button-1>", lambda e, t=text: self._var.set(t))
            item.bind("<Enter>", lambda e, t=text: self._hover(t, True))
            item.bind("<Leave>", lambda e, t=text: self._hover(t, False))
            self._items[text] = item
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _draw(self):
        for text, item in self._items.items():
            on = text == self._var.get()
            item.config(bg=ACCENT if on else BG_ENTRY, fg="#ffffff" if on else FG)

    def _hover(self, text: str, inside: bool):
        if text != self._var.get():
            self._items[text].config(bg=BG_ENTRY_HOVER if inside else BG_ENTRY)


class SearchField(tk.Entry):
    """Seamless search box with a grey “Search” placeholder (as in history)."""

    def __init__(self, parent, placeholder: str = "Search", bg: str = BG, size: int = 12):
        self.var = tk.StringVar()
        super().__init__(parent, textvariable=self.var, font=(FONT, size), bg=bg, fg=FG,
                         insertbackground=FG, relief=tk.FLAT, bd=0, highlightthickness=0)
        self._placeholder_text = placeholder
        self._placeholder = False
        self.bind("<FocusIn>", lambda e: self._focus(True), add="+")
        self.bind("<FocusOut>", lambda e: self._focus(False), add="+")
        self._show_placeholder()

    def value(self) -> str:
        return "" if self._placeholder else self.var.get().strip()

    def clear(self):
        self.var.set("")
        if self.focus_get() is not self:
            self._show_placeholder()

    def _show_placeholder(self):
        if not self.var.get():
            self._placeholder = True
            self.config(fg=FG_SEARCH)
            self.insert(0, self._placeholder_text)

    def _focus(self, focused: bool):
        if focused and self._placeholder:
            self._placeholder = False
            self.delete(0, tk.END)
            self.config(fg=FG)
        elif not focused and not self.var.get():
            self._show_placeholder()


# ─── Scrolling list of cards ─────────────────────────────────

class ScrollArea(tk.Frame):
    """Vertical scrolling frame with the pill scrollbar. Put widgets in .inner."""

    def __init__(self, parent, bg: str = BG):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, takefocus=1)
        bar = PillScrollbar(self, command=self.canvas.yview, bg=bg)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor=tk.NW)
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Wheel scrolls whichever list the pointer is over (not all of them)
        self.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, event):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.canvas.yview_moveto(0)

    def see(self, widget):
        self.update_idletasks()
        total = max(1, self.inner.winfo_height())
        view = self.canvas.winfo_height()
        top = self.canvas.canvasy(0)
        y, h = widget.winfo_y(), widget.winfo_height()
        if y < top:
            self.canvas.yview_moveto(y / total)
        elif y + h > top + view:
            self.canvas.yview_moveto((y + h - view) / total)


class CardList(ScrollArea):
    """
    Clickable cards with one selected. Up/Down move the selection.
    build(frame, item) fills a card; on_select(index) runs on selection.
    selectable(item) → False makes a row a plain heading (not clickable).
    """

    def __init__(self, parent, build, on_select, card_bg: str = BG_ENTRY,
                 bg: str = BG, padx: int = 14, pady: int = 9, gap: int = 2, selectable=None):
        super().__init__(parent, bg=bg)
        self._build, self._on_select, self._selectable = build, on_select, selectable
        self._card_bg, self._padx, self._pady, self._gap = card_bg, px(padx), px(pady), px(gap)
        self._headings: set[int] = set()
        self.cards: list[tk.Frame] = []
        self.selected: int | None = None
        self.canvas.bind("<Up>", lambda e: self._step(-1))
        self.canvas.bind("<Down>", lambda e: self._step(1))

    def set_items(self, items, empty_text: str = ""):
        self.clear()
        self.cards, self.selected, self._headings = [], None, set()
        if not items and empty_text:
            tk.Label(self.inner, text=empty_text, font=(FONT, 10), fg=FG_DIM, bg=self["bg"],
                     justify=tk.CENTER, pady=px(40), wraplength=px(260)).pack(fill=tk.X)
        self.add_items(items)

    def add_items(self, items):
        for i, item in enumerate(items, start=len(self.cards)):
            if self._selectable and not self._selectable(item):
                heading = tk.Frame(self.inner, bg=self["bg"])
                heading.pack(fill=tk.X, padx=(0, px(4)))
                self._build(heading, item)
                set_bg(heading, self["bg"])
                self._headings.add(i)
                self.cards.append(heading)
                continue
            card = tk.Frame(self.inner, bg=self._card_bg, padx=self._padx, pady=self._pady,
                            cursor="hand2")
            card.pack(fill=tk.X, padx=(0, 4), pady=(0, self._gap))
            self._build(card, item)
            for w in (card, *_descendants(card)):
                w.bind("<Button-1>", lambda e, i=i: self.select(i, focus=True))
            card.bind("<Enter>", lambda e, i=i: self._hover(i, True))
            card.bind("<Leave>", lambda e, i=i: self._hover(i, self._inside(e, i)))
            set_bg(card, self._card_bg)
            self.cards.append(card)

    def select(self, i: int, focus: bool = False, notify: bool = True):
        if not (0 <= i < len(self.cards)) or i in self._headings:
            return
        previous, self.selected = self.selected, i
        if previous is not None and previous < len(self.cards):
            set_bg(self.cards[previous], self._card_bg)
        set_bg(self.cards[i], BG_SELECTED)
        self.see(self.cards[i])
        if focus:
            self.canvas.focus_set()
        if notify:
            self._on_select(i)

    def first(self) -> int | None:
        """Index of the first selectable row."""
        return next((i for i in range(len(self.cards)) if i not in self._headings), None)

    def _step(self, delta: int):
        i = self.selected if self.selected is not None else -delta
        while 0 <= i + delta < len(self.cards):
            i += delta
            if i not in self._headings:
                self.select(i)
                return

    def _inside(self, event, i: int) -> bool:
        """Leave also fires when the pointer moves onto a label inside the card."""
        if i >= len(self.cards):
            return False
        w = self.winfo_containing(event.x_root, event.y_root)
        while w is not None:
            if w is self.cards[i]:
                return True
            w = w.master
        return False

    def _hover(self, i: int, inside: bool):
        if i != self.selected and i < len(self.cards):
            set_bg(self.cards[i], BG_ENTRY_HOVER if inside else self._card_bg)


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)
