"""Floating status pill: live level bars while recording, a spinner while working.

The tray icon is too small to tell you whether the mic is actually hearing you,
so this draws a small pill just above the taskbar, on the monitor you are
working on. While recording it shows a scrolling meter: bright bars where you
spoke, dim ones for silence. The dot beside it is green when the LLM cleanup
will run, blue for algorithmic cleanup (chosen in the tray, or Ollama/the model
unreachable), and white for the raw transcript (raw mode, or cleanup switched
off). While Whisper and the cleanup pass run, it shows a
spinner, then a brief check mark once the text is pasted.

Two hard requirements shape the window code:

- **It must never take focus.** The result is pasted with Ctrl+V into whatever
  window is active; if the pill became active, the paste would land in the pill.
  So the window is WS_EX_NOACTIVATE, and it is shown and hidden with
  ShowWindow(SW_SHOWNOACTIVATE / SW_HIDE) rather than Tk's deiconify, which can
  activate it.
- **It must never get in the way.** WS_EX_TRANSPARENT makes it click-through,
  and WS_EX_TOOLWINDOW keeps it off the taskbar and out of Alt+Tab.

Tk is not thread-safe, so the whole UI lives on one dedicated thread. Engine
state hooks fire from other threads and only write plain attributes; the Tk loop
polls them on a ~30 fps timer.
"""

from __future__ import annotations

import ctypes
import logging
import math
import threading
import time
from collections import deque
from ctypes import wintypes

from .config import OverlayConfig
from .engine import Engine, State

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)

# Declared explicitly: ctypes' default int marshalling truncates handles and
# turns HWND_TOPMOST (-1) into 0xFFFFFFFF on x64, and SetWindowPos then fails
# silently - the pill never appears.
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
MONITOR_DEFAULTTONEAREST = 2
#: Per-monitor-v2. Set on the overlay thread only, so the rest of the process
#: (pystray, hotkeys) keeps the DPI behaviour it was written against.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

#: Colour key made fully transparent, giving the pill its rounded ends. Tk does
#: not antialias, so no edge pixels blend with it and there is no fringe.
KEY = "#010203"
BG = "#1d1e22"
TEXT = "#ececee"
DIM = "#4b4d55"
RED = "#ef5350"
BLUE = "#4c9aff"
PURPLE = "#a07cf0"
GREEN = "#43b974"
AMBER = "#e0a33a"
PINK = "#e86fb0"
#: Always-on command mode.
TEAL = "#2ec4b6"
#: Numbered click labels: dark text on yellow reads over any background.
LABEL_BG = "#1d1e22"
LABEL_TEXT = "#ffd60a"
LABEL_EDGE = "#ffd60a"  # outline of what the number clicks
#: Previous-sentence context in the live text.
DIM_TEXT = "#8b8e98"

FRAME_MS = 33
BARS = 28


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _work_area() -> wintypes.RECT:
    """Work area (screen minus taskbar) of the monitor holding the active window."""
    monitor = user32.MonitorFromWindow(user32.GetForegroundWindow(), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    return info.rcWork


def _short_error(detail: str) -> str:
    """Engine error details are written for the log; the pill fits ~34 chars."""
    d = detail.lower()
    if d.startswith("input too quiet"):
        return "Mic too quiet - try Mic boost"
    if d.startswith("no signal"):
        return "No signal from the mic"
    if d.startswith("no audio captured"):
        return "No audio captured"
    if d.startswith("could not insert"):
        # Only reached when the clipboard fallback failed too (see inject.py).
        return "Couldn't insert the text"
    if "window you started in" in d:
        return "Window gone - text on clipboard"
    if "could not be downloaded" in d:
        return "Speech model not downloaded"
    return detail or "Something went wrong"


def _rounded(c, x0: float, y0: float, x1: float, y1: float, r: float) -> None:
    """Filled rounded rectangle; r = half the height gives the classic pill."""
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    for ox, oy in ((x0, y0), (x1 - 2 * r, y0), (x0, y1 - 2 * r), (x1 - 2 * r, y1 - 2 * r)):
        c.create_oval(ox, oy, ox + 2 * r, oy + 2 * r, fill=BG, outline=BG)
    c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=BG, outline=BG)
    c.create_rectangle(x0, y0 + r, x1, y1 - r, fill=BG, outline=BG)


def _wrap(tokens: list[tuple[str, str]], font, width: float) -> list:
    """Greedy word wrap with per-word colours: [[(x, word, colour, font), ...], ...]."""
    space = font.measure(" ")
    lines: list = [[]]
    x = 0.0
    for word, colour in tokens:
        wl = font.measure(word)
        if lines[-1] and x + space + wl > width:
            lines.append([])
            x = 0.0
        if lines[-1]:
            x += space
        lines[-1].append((x, word, colour, font))
        x += wl
    return lines


class Overlay:
    def __init__(self, engine: Engine, cfg: OverlayConfig, live=None):
        self.engine = engine
        self.cfg = cfg
        self.live = live  # LivePreview, or None when live text is off
        self._layout_key = None
        self._layout: tuple[list, int] = ([], 0)
        # Written by engine hooks on any thread, read by the Tk thread.
        self._mode = "hidden"  # hidden | recording | busy | done | message | command
        self._label = ""
        self._colour = TEXT
        self._since = time.monotonic()
        self._hide_at: float | None = None
        self._was_injecting = False
        #: Can the cleanup LLM run? Polled off the Tk thread; None = not yet known.
        self._llm_ready: bool | None = None
        self._poke = threading.Event()
        engine.on_state(self._on_state)
        engine.on_notice(self._on_notice)
        self._glyph = "!"
        # Numbered click labels (command mode), drawn by the Tk thread.
        self._labels: list = []
        self._labels_version = 0
        self._labels_drawn = 0
        self._labels_big = False
        self._list: list = []
        self._list_version = 0
        self._list_drawn = 0
        from . import desktop

        desktop.label_sink = self.set_labels
        desktop.list_sink = self.set_list

    def set_labels(self, labels: list, big: bool = False) -> None:
        """[(number, (left, top, right, bottom)), ...] in screen pixels; [] clears.
        big: one large number filling each box (monitor numbers)."""
        self._labels = list(labels)
        self._labels_big = big
        self._labels_version += 1

    def set_list(self, lines: list) -> None:
        """The window list: [(number, "text"), ...]; [] hides it."""
        self._list = list(lines)
        self._list_version += 1

    # -- engine side -------------------------------------------------------

    def _on_state(self, state: State, detail: str) -> None:
        now = time.monotonic()
        mode, label, colour, hide_at = "hidden", "", TEXT, None

        if state == State.RECORDING:
            mode = "recording"
            self._poke.set()  # re-check the LLM now, not up to 15s from now
        elif state == State.TRANSCRIBING:
            mode, label, colour = "busy", "Transcribing…", BLUE
        elif state == State.CLEANING:
            mode, label, colour = "busy", "Cleaning up…", PURPLE
        elif state == State.STYLING:
            mode, label, colour = "busy", f"Styling: {detail}…", PINK
        elif state == State.INJECTING:
            if self.engine.stylizer.last_outcome in ("rejected", "failed"):
                # The rewrite was refused by the guard (or Ollama failed), so the
                # plain cleaned text went in. Say so - otherwise it looks ignored.
                mode, label, colour = "done", "Inserted - style skipped", AMBER
                hide_at = now + 3.0
            elif self.engine.cleaner.last_outcome == "failed":
                # The LLM was wanted but Ollama could not do it, so the rules
                # cleaned it instead. Say so rather than fail silently.
                mode, label, colour = "done", "Inserted - LLM off, basic cleanup", AMBER
                hide_at = now + 3.0
            else:
                mode, label, colour = "done", "Inserted", GREEN
                hide_at = now + 1.5
        elif state == State.IDLE and self._was_injecting:
            # The success label is already up; let it run out its timer.
            self._was_injecting = False
            return
        elif state == State.IDLE and detail == "no speech detected":
            mode, label, colour, hide_at = "message", "No speech detected", AMBER, now + 2.5
        elif state == State.COMMAND:
            # Stays up for as long as command mode lasts.
            mode, label, colour = "command", detail or "Command mode", TEAL
        elif state == State.ERROR:
            mode, label, colour, hide_at = "message", _short_error(detail), RED, now + 4.0
        # IDLE otherwise (cancelled, ready) and LOADING: stay out of the way.

        self._was_injecting = state == State.INJECTING
        if mode != self._mode or mode != "recording":
            self._since = now
        self._mode, self._label, self._colour, self._hide_at = mode, label, colour, hide_at

    def _on_notice(self, text: str, kind: str, seconds: float) -> None:
        if self._mode in ("recording", "busy"):
            return  # never over a dictation; the toast still shows
        colour, glyph = {"ok": (GREEN, "✓"), "error": (RED, "!")}.get(kind, (BLUE, "i"))
        self._since = time.monotonic()
        self._mode, self._label, self._colour = "notice", text, colour
        self._glyph = glyph
        self._hide_at = time.monotonic() + seconds

    # -- Tk side -----------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="localflow-overlay").start()
        threading.Thread(target=self._poll_llm, daemon=True, name="localflow-llm-check").start()

    def _poll_llm(self) -> None:
        """Keep _llm_ready fresh. A 1.5s-timeout GET; never on the Tk thread."""
        while True:
            try:
                ready = self.engine.cleaner.ready()
            except Exception:
                ready = False
            if ready != self._llm_ready:
                log.info("cleanup LLM %s", "available" if ready else "unavailable")
            self._llm_ready = ready
            self._poke.wait(15)
            self._poke.clear()

    def _run(self) -> None:
        try:
            self._run_tk()
        except Exception:
            # The overlay is a nicety. Dictation must keep working without it.
            log.exception("overlay failed; continuing without it")

    def _run_tk(self) -> None:
        import tkinter as tk

        try:
            user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            scale = user32.GetDpiForSystem() / 96
        except (AttributeError, OSError):
            scale = 1.0
        self.s = s = scale
        self.w, self.h = int(250 * s), int(42 * s)
        # Room under the waveform for two lines of live text. The window is
        # always this tall (transparent where unused); the pill grows upward.
        self.text_area = int(44 * s) if self.live else 0
        self.H = self.h + self.text_area

        root = tk.Tk()
        root.overrideredirect(True)
        root.configure(bg=KEY)
        root.attributes("-transparentcolor", KEY)
        root.attributes("-alpha", 1.0)
        root.attributes("-topmost", True)
        # Park it off-screen for its one Tk-driven map, then take over show/hide.
        root.geometry(f"{self.w}x{self.H}+-32000+-32000")
        self.canvas = tk.Canvas(root, width=self.w, height=self.H, bg=KEY, highlightthickness=0)
        self.canvas.pack()
        root.update()

        self.hwnd = user32.GetParent(root.winfo_id())
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            self.hwnd,
            GWL_EXSTYLE,
            ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
        )
        user32.ShowWindow(self.hwnd, SW_HIDE)
        self.visible = False
        self.font = ("Segoe UI", -int(13 * s))
        self.font_bold = ("Segoe UI Semibold", -int(13 * s))
        import tkinter.font as tkfont

        # Live text shrinks through these sizes before it starts dropping words.
        self.live_fonts = [tkfont.Font(root=root, family="Segoe UI", size=-int(px * s)) for px in (13, 12, 11, 10)]
        self.levels: deque[float] = deque([0.0] * BARS, maxlen=BARS)
        self.root = root
        self._make_label_layer(tk)
        self._make_list_layer(tk)
        log.info("overlay ready")

        root.after(FRAME_MS, self._tick)
        root.mainloop()

    def _show(self) -> None:
        if self.visible:
            return
        area = _work_area()
        x = (area.left + area.right - self.w) // 2
        y = area.bottom - self.H - int(self.cfg.margin * self.s)
        if not user32.SetWindowPos(
            self.hwnd, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        ):
            log.warning("overlay SetWindowPos failed (error %d)", ctypes.get_last_error())
        self.visible = True

    def _hide(self) -> None:
        if self.visible:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.visible = False
            self.levels.extend([0.0] * BARS)

    def _live_layout(self) -> tuple[list, int]:
        """Word-wrapped live text: ([[(x, word, colour, font), ...] per line], line height).

        Cached per preview update. Fits in two lines by stepping the font down,
        then by dropping the oldest words behind a leading "...".
        """
        live = self.live
        if live is None or not live.text:
            return [], 0
        key = (live.version, self._mode == "recording")
        if key == self._layout_key:
            return self._layout

        from .live import display_words

        context, current, cut = display_words(
            live.text, self.cfg.live_context_words, self.cfg.live_max_words
        )
        tokens = [(word, DIM_TEXT) for word in context] + [(word, TEXT) for word in current]
        if cut:
            tokens.insert(0, ("…", DIM_TEXT))
        width = self.w - 36 * self.s

        lines: list = []
        font = self.live_fonts[-1]
        for font in self.live_fonts:
            lines = _wrap(tokens, font, width)
            if len(lines) <= 2:
                break
        while len(lines) > 2 and len(tokens) > 1:
            # Smallest size still overflows: drop the oldest word, mark the cut.
            body = tokens[1:] if tokens[0][0] == "…" else tokens
            tokens = [("…", DIM_TEXT)] + body[1:]
            lines = _wrap(tokens, font, width)

        self._layout_key = key
        self._layout = (lines, font.metrics("linespace"))
        return self._layout

    # -- numbered labels ---------------------------------------------------

    def _make_label_layer(self, tk) -> None:
        """A second click-through, never-focused window for the labels, made
        once (like the pill) and moved over the target window when needed."""
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.configure(bg=KEY)
        win.attributes("-transparentcolor", KEY)
        win.attributes("-alpha", self.cfg.label_opacity)  # see what's under them
        win.attributes("-topmost", True)
        win.geometry("10x10+-32000+-32000")
        self.label_canvas = tk.Canvas(win, width=10, height=10, bg=KEY, highlightthickness=0)
        self.label_canvas.pack(fill="both", expand=True)
        win.update()
        self.label_hwnd = user32.GetParent(win.winfo_id())
        ex = user32.GetWindowLongW(self.label_hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            self.label_hwnd, GWL_EXSTYLE,
            ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
        )
        user32.ShowWindow(self.label_hwnd, SW_HIDE)
        self.label_win = win
        self.label_font = ("Segoe UI Semibold", -int(12 * self.s))

    def _draw_labels(self) -> None:
        self._labels_drawn = self._labels_version
        labels = self._labels
        c = self.label_canvas
        c.delete("all")
        if not labels:
            user32.ShowWindow(self.label_hwnd, SW_HIDE)
            return
        pad = int(30 * self.s)
        x0 = min(r[0] for _, r in labels) - pad
        y0 = min(r[1] for _, r in labels) - pad
        x1 = max(r[2] for _, r in labels) + pad
        y1 = max(r[3] for _, r in labels) + pad
        w, h = x1 - x0, y1 - y0
        c.config(width=w, height=h)
        s = self.s
        bh = int(17 * s)
        for n, (left, top, right, bottom) in labels:
            text = str(n)
            if self._labels_big:
                # A screen's number, big, in the middle of it.
                c.create_rectangle(left - x0, top - y0, right - x0, bottom - y0,
                                   fill=LABEL_BG, outline=LABEL_EDGE, width=max(2, int(3 * s)))
                c.create_text((left + right) / 2 - x0, (top + bottom) / 2 - y0, text=text,
                              fill=LABEL_TEXT, font=("Segoe UI Semibold", -int((bottom - top) * 0.6)))
                continue
            bw = int((9 + 7 * len(text)) * s)
            # Top-left corner of the control, nudged inside it, so the label
            # sits on what it names without hiding all of it.
            x = left - x0 + int(2 * s)
            y = top - y0 + int(2 * s)
            # The control's own outline, so it's clear what the number clicks.
            c.create_rectangle(left - x0, top - y0, right - x0 - 1, bottom - y0 - 1,
                               outline=LABEL_EDGE, width=1)
            c.create_rectangle(x, y, x + bw, y + bh, fill=LABEL_BG, outline=LABEL_EDGE)
            c.create_text(x + bw / 2, y + bh / 2, text=text, fill=LABEL_TEXT, font=self.label_font)
        if not user32.SetWindowPos(
            self.label_hwnd, HWND_TOPMOST, x0, y0, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW
        ):
            log.warning("label layer SetWindowPos failed (error %d)", ctypes.get_last_error())

    # -- the window list ----------------------------------------------------

    def _make_list_layer(self, tk) -> None:
        """Like the labels: click-through, never focused, made once."""
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.configure(bg=KEY)
        win.attributes("-transparentcolor", KEY)
        win.attributes("-alpha", 0.98)  # solid: text behind it made it hard to read
        win.attributes("-topmost", True)
        win.geometry("10x10+-32000+-32000")
        self.list_canvas = tk.Canvas(win, width=10, height=10, bg=KEY, highlightthickness=0)
        self.list_canvas.pack(fill="both", expand=True)
        win.update()
        self.list_hwnd = user32.GetParent(win.winfo_id())
        ex = user32.GetWindowLongW(self.list_hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            self.list_hwnd, GWL_EXSTYLE,
            ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
        )
        user32.ShowWindow(self.list_hwnd, SW_HIDE)
        self.list_win = win

    def _draw_list(self) -> None:
        self._list_drawn = self._list_version
        lines = self._list
        c = self.list_canvas
        c.delete("all")
        if not lines:
            user32.ShowWindow(self.list_hwnd, SW_HIDE)
            return
        s = self.s
        row, pad = int(30 * s), int(18 * s)
        head = int(34 * s)
        w = int(820 * s)
        h = head + row * len(lines) + pad
        c.config(width=w, height=h)
        _rounded(c, 0, 0, w - 1, h - 1, 14 * s)
        c.create_text(pad, pad, anchor="nw", fill=DIM_TEXT, font=self.font,
                      text='Windows - say "switch to" and a number, or "close 3", "move 2 to the left"')
        for i, (n, text) in enumerate(lines):
            y = head + pad // 2 + i * row
            chip = int(28 * s)
            c.create_rectangle(pad, y, pad + chip, y + row - int(6 * s), fill=TEAL, outline=TEAL)
            c.create_text(pad + chip / 2, y + (row - int(6 * s)) / 2, text=str(n), fill=BG,
                          font=self.font_bold)
            label = text if len(text) <= 96 else text[:95] + "…"
            c.create_text(pad + chip + int(12 * s), y + (row - int(6 * s)) / 2, text=label,
                          anchor="w", fill=TEXT, font=self.font)
        area = _work_area()
        x = (area.left + area.right - w) // 2
        y = (area.top + area.bottom - h) // 2
        user32.SetWindowPos(self.list_hwnd, HWND_TOPMOST, x, y, w, h,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _tick(self) -> None:
        if self._list_version != self._list_drawn:
            try:
                self._draw_list()
            except Exception:
                log.warning("drawing the window list failed", exc_info=True)
        if self._labels_version != self._labels_drawn:
            try:
                self._draw_labels()
            except Exception:
                log.warning("drawing labels failed", exc_info=True)
        try:
            self._frame()
        except Exception:
            log.debug("overlay frame failed", exc_info=True)
        self.root.after(FRAME_MS, self._tick)

    def _frame(self) -> None:
        mode = self._mode
        if self._hide_at is not None and time.monotonic() >= self._hide_at:
            self._mode = mode = "hidden"
            self._hide_at = None
        if mode == "hidden":
            self._hide()
            return

        c, s, w, h, H = self.canvas, self.s, self.w, self.h, self.H
        c.delete("all")
        lines, line_h = self._live_layout() if mode in ("recording", "busy") else ([], 0)
        text_h = len(lines) * line_h + int(8 * s) if lines else 0
        y0 = H - h - text_h  # the pill is bottom-anchored and grows upward
        _rounded(c, 0, y0, w - 1, H - 1, h / 2 if not lines else 16 * s)
        mid = y0 + h / 2
        elapsed = time.monotonic() - self._since

        for row, words in enumerate(lines):
            y = y0 + h - 4 * s + row * line_h
            for x, word, colour, font in words:
                c.create_text(18 * s + x, y, text=word, anchor="nw", fill=colour, font=font)

        if mode == "recording":
            self.levels.append(self.engine.recorder.level)
            # Pulsing dot: green = LLM cleanup, blue = algorithmic cleanup (by
            # choice, or because the LLM is unavailable), white = raw.
            if not self.engine.cleanup_this_run:
                dot = TEXT
            elif self.engine.cleaner.uses_llm and self._llm_ready:
                dot = GREEN
            else:
                dot = BLUE
            r = (4.5 + 1.2 * math.sin(elapsed * 5)) * s
            cx = 22 * s
            c.create_oval(cx - r, mid - r, cx + r, mid + r, fill=dot, outline=dot)
            # Level bars, newest on the right.
            x0, x1 = 38 * s, w - 58 * s
            step = (x1 - x0) / BARS
            bar_w = max(2, int(step * 0.55))
            max_h = h - 16 * s
            for i, level in enumerate(self.levels):
                db = 20 * math.log10(max(level, 1e-5))
                t = min(max((db + 60) / 40, 0.0), 1.0)  # -60 dB .. -20 dB
                bh = max(2 * s, t * max_h)
                x = x0 + i * step
                colour = TEXT if level >= self.cfg.speech_rms else DIM
                c.create_rectangle(x, mid - bh / 2, x + bar_w, mid + bh / 2, fill=colour, outline=colour)
            secs = int(elapsed)
            c.create_text(
                w - 18 * s, mid, text=f"{secs // 60}:{secs % 60:02d}",
                anchor="e", fill=TEXT, font=self.font,
            )
        else:
            cx, r = 24 * s, 8 * s
            if mode == "busy":
                start = (-elapsed * 360) % 360
                c.create_oval(cx - r, mid - r, cx + r, mid + r, outline=DIM, width=max(2, int(2.5 * s)))
                c.create_arc(
                    cx - r, mid - r, cx + r, mid + r, start=start, extent=110,
                    style="arc", outline=self._colour, width=max(2, int(2.5 * s)),
                )
            elif mode == "done":
                c.create_oval(cx - r, mid - r, cx + r, mid + r, fill=self._colour, outline=self._colour)
                c.create_line(
                    cx - 4 * s, mid, cx - 1 * s, mid + 3.5 * s, cx + 4.5 * s, mid - 3.5 * s,
                    fill=BG, width=max(2, int(2 * s)),
                )
            elif mode == "command":
                # Breathing ring: listening for a command.
                rr = r * (0.85 + 0.15 * math.sin(elapsed * 3))
                c.create_oval(cx - rr, mid - rr, cx + rr, mid + rr, outline=self._colour,
                              width=max(2, int(2.5 * s)))
                c.create_text(cx, mid, text="›", fill=self._colour, font=self.font_bold)
            elif mode == "notice":
                c.create_oval(cx - r, mid - r, cx + r, mid + r, fill=self._colour, outline=self._colour)
                c.create_text(cx, mid, text=self._glyph, fill=BG, font=self.font_bold)
            else:  # message
                c.create_oval(cx - r, mid - r, cx + r, mid + r, fill=self._colour, outline=self._colour)
                c.create_text(cx, mid, text="!", fill=BG, font=self.font_bold)
            label = self._label if len(self._label) <= 34 else self._label[:33] + "…"
            c.create_text(40 * s, mid, text=label, anchor="w", fill=TEXT, font=self.font)

        self._show()
