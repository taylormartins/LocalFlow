"""Voice commands: what can be said, how it's understood, what it does.

Understanding is algorithmic first. Every command is a handful of spoken
patterns ("switch to {name}", "minimize [the] [window]"), matched word by
word with the same fuzzy alignment as the always-on phrases, so a slightly
misheard word still counts but a different sentence doesn't. Nothing here
needs an LLM.

When the cleanup LLM is available, it is an optional second layer in command
mode: a request the patterns don't cover ("put Spotify away", "reply that I'm
running late") is turned into steps chosen from this same list, and only from
it. The LLM can't invent an action, and a plan that closes or sends
something waits for a spoken "yes" first.

Commands run on one worker thread, in order: UI Automation is COM, and
COM objects belong to the thread that made them.
"""

from __future__ import annotations

import difflib
import functools
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pathlib import Path

from . import desktop
from .config import PROJECT_ROOT
from .wake import _aligned, normalise

#: Phrases learned from missed commands, on this PC only (gitignored).
LEARNED = PROJECT_ROOT / "learned.json"

log = logging.getLogger(__name__)

#: Every fixed word of a pattern must be at least this close to what was heard.
ACCEPT = 0.8
#: Longer than this, a burst is talk, not a command (except "type ...").
MAX_COMMAND_WORDS = 16
#: A spoken name must fit a window, app or button at least this well.
NAME_ACCEPT = 0.72
#: Said around a command without changing it.
LEAD = {"okay", "ok", "please", "now", "and", "then", "so", "um", "uh", "hey", "can", "you",
        "could", "would", "just"}
TRAIL = {"please", "thanks", "now", "for", "me"}
#: What Whisper "hears" in a click, a cue or a breath: never a command.
NOISE = {"you", "thank you", "thanks", "bye", "okay", "thank you for watching", "so", "uh", "um"}


class CommandError(Exception):
    """A command that was understood but couldn't be carried out."""


@dataclass
class Ctx:
    """What an action may touch, supplied by the listener."""
    #: Text LocalFlow pasted last, when, and into which window (scratch that).
    last_text: str = ""
    last_at: float = 0.0
    last_target: object = None
    paste: Callable[[str], None] | None = None
    #: The last dictation even if scratched (for "paste that again").
    last_any: str = ""
    #: A numbered label named by the command ("type into 12 ...").
    label: str = ""
    in_target: Callable[[object, Callable[[], None]], None] | None = None


@dataclass
class Command:
    name: str
    patterns: list[str]
    #: action(arg, ctx) -> what happened, for the pill ("Switched to Chrome").
    action: Callable[[str, Ctx], str]
    help: str
    #: Acts on the window just dictated into: run there after a dictation.
    keys: bool = False
    #: Closes or sends something: an LLM plan with this waits for "yes".
    risky: bool = False
    #: The slot is free text (typed as is), not a name to look up.
    text_slot: bool = False
    #: The slot names the window to do it in ("new Chrome tab"), not an argument.
    aim: bool = False
    #: Runs only once this is said next ("goodnight"), after a triple click.
    confirm: list[str] = field(default_factory=list)


@dataclass
class Parsed:
    command: Command
    arg: str
    score: float
    #: A window to bring forward first ("new tab in Chrome"), or "".
    where: str = ""
    #: A numbered label to act on ("type into 12 ..."), or "".
    label: str = ""

    @property
    def size(self) -> int:
        """Words in the slots: ties go to the reading that leaves fewer."""
        return len(self.arg.split()) + len(self.where.split()) + len(self.label.split())

    def __str__(self) -> str:
        s = f"{self.command.name}({self.arg})" if self.arg else self.command.name
        return f"{s} in {self.where}" if self.where else s


# -- actions ---------------------------------------------------------------

def _keys(chord: str, label: str, times: int = 1, delay: float = 0.0
          ) -> Callable[[str, Ctx], str]:
    def run(arg: str, ctx: Ctx) -> str:
        if delay:
            # Longer for a long paste the app is still taking in: ~0.4 s for
            # 1000 characters, up to 0.8 s.
            recent = ctx.last_any if time.time() - ctx.last_at < 15 else ""
            time.sleep(min(0.8, delay + len(recent) / 4000))
        desktop.press(chord, times)
        return label
    return run


def _paste_keys(arg: str, ctx: Ctx) -> str:
    desktop.press("ctrl+v")
    # "Paste" said out loud is the answer to Windows Terminal's "are you sure?"
    if desktop.confirm_terminal_paste(timeout=0.8):
        return "Pasted (said yes to the terminal)"
    return "Pasted"


def _scratch(arg: str, ctx: Ctx) -> str:
    if not ctx.last_text or time.time() - ctx.last_at > 120:
        raise CommandError("nothing recent to scratch")
    n = len(ctx.last_text.replace("\r\n", "\n"))
    text, ctx.last_text = ctx.last_text, ""

    def wipe() -> None:
        desktop.press("backspace", n)

    if ctx.in_target is not None:
        ctx.in_target(ctx.last_target, wipe)
    else:
        wipe()
    return f"Deleted {len(text.split())} words"


def _switch(arg: str, ctx: Ctx) -> str:
    picked = number(arg) is not None and bool(desktop.numbered)
    w, score = desktop.find_window(arg)
    if picked:
        hide_windows()
    if w is None or score < NAME_ACCEPT:
        raise CommandError(f"no window called {arg}")
    if not desktop.focus(w):
        raise CommandError(f"{w.label} wouldn't come forward")
    return f"Switched to {_shown(w, arg)}"


def _open(arg: str, ctx: Ctx) -> str:
    site = re.fullmatch(r"(.+?) ?(?:dot|\.) ?(com|org|net|io|co uk|dev|ai|gov|edu)", arg)
    if site:
        domain = site.group(1).replace(" ", "") + "." + site.group(2).replace(" ", ".")
        desktop.open_url("https://" + domain)
        return f"Opened {domain}"
    w, wscore = desktop.find_window(arg)
    app, ascore = desktop.find_app(arg)
    if w is not None and wscore >= max(NAME_ACCEPT, ascore - 0.05):
        if desktop.focus(w):
            return f"Switched to {_shown(w, arg)}"
    if app is None or ascore < NAME_ACCEPT:
        raise CommandError(f"no app called {arg}")
    before = desktop.foreground()
    desktop.launch(app)
    # Wait for it to come up, so a following step ("...and type hello")
    # lands in the new window rather than the old one.
    deadline = time.monotonic() + 6.0
    while desktop.foreground() == before and time.monotonic() < deadline:
        time.sleep(0.1)
    return f"Opened {app.name}"


def _window_op(how: int, label: str) -> Callable[[str, Ctx], str]:
    def run(arg: str, ctx: Ctx) -> str:
        if arg:
            w, score = desktop.find_window(arg)
            if w is None or score < NAME_ACCEPT:
                raise CommandError(f"no window called {arg}")
            hwnd, what = w.hwnd, _shown(w, arg)
        else:
            hwnd, what = desktop.foreground(), "window"
        if not hwnd:
            raise CommandError("no window in front")
        if how == -1:
            desktop.close(hwnd)
        else:
            desktop.show(hwnd, how)
        return f"{label} {what}"
    return run


number = desktop.number


#: The controls numbered on screen now, label n = _labelled[n - 1].
_labelled: list[desktop.Control] = []
MAX_LABELS = 300


def label_targets(items: list[desktop.Control], bounds: tuple[int, int, int, int]
                  ) -> list[desktop.Control]:
    """What gets a number: inside the window, one per spot, in reading order.

    A control mostly inside another clickable one of similar size is the same
    target twice (a link and the box around it), so only the outer one is
    kept. A small link inside a big panel keeps its own number.
    """
    bl, bt, br, bb = bounds
    items = [c for c in items
             if c.rect[0] < br and c.rect[2] > bl and c.rect[1] < bb and c.rect[3] > bt]

    def area(r):
        return max(1, (r[2] - r[0]) * (r[3] - r[1]))

    items.sort(key=lambda c: -area(c.rect))
    kept: list[desktop.Control] = []
    for c in items:
        l, t, r, b = c.rect
        inside = False
        for k in kept:
            kl, kt, kr, kb = k.rect
            if l >= kl - 3 and t >= kt - 3 and r <= kr + 3 and b <= kb + 3 and area(k.rect) < 4 * area(c.rect):
                inside = True
                break
        if not inside:
            kept.append(c)
    kept.sort(key=lambda c: (c.rect[1] // 12, c.rect[0]))
    return kept[:MAX_LABELS]


def _place(where: str, on_monitor: bool = False) -> Callable[[str, Ctx], str]:
    def run(arg: str, ctx: Ctx) -> str:
        if on_monitor:
            # "...top right of monitor 2": the slot is the monitor's number.
            n = number(arg)
            if n is None:
                raise CommandError(f"which screen is {arg}?")
            hwnd = desktop.foreground()
            if not hwnd:
                raise CommandError("no window in front")
            try:
                desktop.place(hwnd, where, monitor=n)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            return f"Moved to the {where} of screen {n}"
        if arg:
            w, score = desktop.find_window(arg)
            if w is None or score < NAME_ACCEPT:
                raise CommandError(f"no window called {arg}")
            hwnd, what = w.hwnd, _shown(w, arg)
        else:
            hwnd, what = desktop.foreground(), "window"
        if not hwnd:
            raise CommandError("no window in front")
        desktop.place(hwnd, where)
        return f"Moved {what} to the {where}" if where != "full" else f"{what} fills the screen"
    return run


_list_at = [0.0]


def list_shown_at() -> float:
    return _list_at[0]


def _show_windows(arg: str, ctx: Ctx) -> str:
    wins = desktop.windows()[:20]
    if not wins:
        raise CommandError("no windows open")
    if not desktop.show_window_list(wins):
        raise CommandError("the window list needs the overlay (overlay.enabled)")
    _list_at[0] = time.time()
    return f"{len(wins)} windows - say switch to and a number"


def hide_windows(arg: str = "", ctx: Ctx | None = None) -> str:
    _list_at[0] = 0.0
    desktop.hide_window_list()
    if not _labelled and not _grid:
        desktop.show_labels([])  # the chips
    return "Window list hidden"


def _to_monitor(arg: str, ctx: Ctx) -> str:
    n = number(arg)
    if n is None:
        raise CommandError(f"which screen is {arg}?")
    hwnd = desktop.foreground()
    if not hwnd:
        raise CommandError("no window in front")
    try:
        desktop.to_monitor(hwnd, n)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    return f"Moved to screen {n}"


def _show_monitors(arg: str, ctx: Ctx) -> str:
    mons = desktop.monitors()
    labels = []
    for i, (_, whole, _) in enumerate(mons):
        cx, cy = (whole.left + whole.right) // 2, (whole.top + whole.bottom) // 2
        half = max(90, (whole.right - whole.left) // 16)
        labels.append((i + 1, (cx - half, cy - half, cx + half, cy + half)))
    desktop.show_labels(labels, "big")

    def clear() -> None:
        time.sleep(4.0)
        if not _labelled:
            desktop.show_labels([])

    threading.Thread(target=clear, daemon=True, name="localflow-monitor-numbers").start()
    return f"{len(mons)} screens, numbered left to right"


def _show_labels(arg: str, ctx: Ctx) -> str:
    hwnd = desktop.foreground()
    if not hwnd:
        raise CommandError("no window in front")
    unique = label_targets(desktop.controls(hwnd, named=False), desktop.window_rect(hwnd))
    if not unique:
        raise CommandError("nothing clickable found")
    _labelled[:] = unique
    if not desktop.show_labels([(i + 1, c.rect) for i, c in enumerate(unique)]):
        _labelled.clear()
        raise CommandError("labels need the overlay (overlay.enabled)")
    return f"{len(unique)} labels - say click and a number"


def _refresh_labels(after: float = 0.7) -> str:
    """Labels stay up: after a click, number what's there now (a click often
    opens a menu or a page)."""
    time.sleep(after)
    try:
        return _show_labels("", Ctx())
    except CommandError:
        hide_labels()
        return "nothing more to click"


# -- the grid (for apps that don't name their buttons) ----------------------------

#: The area the grid covers now; [] when it isn't up.
_grid: list[tuple[int, int, int, int]] = []


def _cells(rect: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
    """3x3 cells, numbered left to right, top to bottom."""
    left, top, right, bottom = rect
    w, h = (right - left) / 3, (bottom - top) / 3
    return [(int(left + c * w), int(top + r * h), int(left + (c + 1) * w), int(top + (r + 1) * h))
            for r in range(3) for c in range(3)]


def _draw_grid() -> None:
    desktop.show_labels([(i + 1, cell) for i, cell in enumerate(_cells(_grid[0]))], "grid")


def _show_grid(arg: str, ctx: Ctx) -> str:
    hwnd = desktop.foreground()
    if not hwnd:
        raise CommandError("no window in front")
    _labelled.clear()
    _grid[:] = [desktop.window_rect(hwnd)]
    _draw_grid()
    return "Grid: say a number to zoom in, or click and a number"


def grid_up() -> bool:
    return bool(_grid)


def grid_zoom(n: int) -> str:
    """Zoom into cell n: the grid now covers just that cell."""
    cells = _cells(_grid[0])
    if not 1 <= n <= len(cells):
        raise CommandError(f"no cell {n}")
    cell = cells[n - 1]
    if cell[2] - cell[0] < 12 or cell[3] - cell[1] < 12:
        raise CommandError("as small as it goes - say click and a number")
    _grid[0] = cell
    _draw_grid()
    return f"Zoomed into {n}"


def hide_grid(arg: str = "", ctx: Ctx | None = None) -> str:
    _grid.clear()
    desktop.show_labels([])
    return "Grid hidden"


def _grid_click(n: int | None, button: str = "left", double: bool = False) -> str:
    rect = _grid[0] if n is None else _cells(_grid[0])[n - 1]
    hide_grid()
    time.sleep(0.05)
    desktop.click_at((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2, button, double)
    return f"Clicked {n}" if n else "Clicked"


def hide_labels(arg: str = "", ctx: Ctx | None = None) -> str:
    had = bool(_labelled)
    _labelled.clear()
    desktop.show_labels([])
    return "Labels hidden" if had else "No labels up"


def _click_kind(button: str = "left", double: bool = False) -> Callable[[str, Ctx], str]:
    def run(arg: str, ctx: Ctx) -> str:
        n = number(arg) if arg else None
        if _grid and (n is not None or not arg):
            if n is not None and not 1 <= n <= 9:
                raise CommandError(f"no cell {n}")
            return _grid_click(n, button, double)
        if n is not None and _labelled:
            if not 1 <= n <= len(_labelled):
                raise CommandError(f"no label {n}")
            c = _labelled[n - 1]
            l, t, r, b = c.rect
            if button == "left" and not double:
                desktop.activate_control(c)
            else:
                desktop.click_at((l + r) // 2, (t + b) // 2, button, double)
            # Labels stay up: number whatever the click opened.
            now = _refresh_labels()
            return f"Clicked {n}" + (f" ({c.name[:20]})" if c.name else "") + f" - {now}"
        if n is not None and desktop.numbered:
            return _switch(str(n), ctx)  # a window from "show windows"
        if n is not None:
            raise CommandError("say 'show labels' or 'show grid' first")
        if not arg:
            raise CommandError("click what?")
        return _click_named(arg, ctx)
    return run


def _type_into(arg: str, ctx: Ctx) -> str:
    """Type text into a numbered label's field: "type into 12 hello there"."""
    if ctx.paste is None:
        raise CommandError("can't type here")
    label, text = ctx.label, arg
    n = number(label)
    if n is None:  # "twelve hello" taken as the label: keep the number part
        words = label.split()
        for k in range(len(words) - 1, 0, -1):
            if number(" ".join(words[:k])) is not None:
                n, text = number(" ".join(words[:k])), " ".join(words[k:] + [text])
                break
    if n is None:
        raise CommandError(f"which field is {label}?")
    if not _labelled:
        raise CommandError("say 'show labels' first")
    if not 1 <= n <= len(_labelled):
        raise CommandError(f"no label {n}")
    c = _labelled[n - 1]
    try:
        c.element.SetFocus()
    except Exception:
        l, t, r, b = c.rect
        desktop.click_at((l + r) // 2, (t + b) // 2)
    time.sleep(0.15)
    ctx.paste(text)
    now = _refresh_labels(0.4)
    return f"Typed into {n} - {now}"


def _click_named(arg: str, ctx: Ctx) -> str:
    """Click a control by its name ("click Send")."""
    hwnd = desktop.foreground()
    if not hwnd:
        raise CommandError("no window in front")
    items = desktop.controls(hwnd)
    c, score = desktop.find_control(arg, items)
    if c is None or score < NAME_ACCEPT:
        raise CommandError(f"no button called {arg}")
    desktop.activate_control(c)
    return f"Clicked {c.name[:30]}"


def _center(arg: str, ctx: Ctx) -> str:
    if arg:
        w, score = desktop.find_window(arg)
        if w is None or score < NAME_ACCEPT:
            raise CommandError(f"no window called {arg}")
        desktop.focus(w)
        hwnd, what = w.hwnd, _shown(w, arg)
    else:
        hwnd, what = desktop.foreground(), "window"
    if not hwnd:
        raise CommandError("no window in front")
    desktop.center(hwnd)
    return f"Centred {what}"


def _history(arg: str, ctx: Ctx) -> str:
    from .tray import open_history

    open_history()
    return "Opening dictation history"


def _paste_again(arg: str, ctx: Ctx) -> str:
    if ctx.paste is None or not ctx.last_any:
        raise CommandError("nothing dictated yet")
    ctx.paste(ctx.last_any)
    return f"Pasted again ({len(ctx.last_any.split())} words)"


# -- undo ----------------------------------------------------------------------

#: Commands that move or resize a window: undone by putting it back exactly.
MOVES = {"center", "next_monitor", "previous_monitor", "maximize", "minimize", "restore",
         "to_monitor", "place_top_left_of", "place_top_right_of", "place_bottom_left_of",
         "place_bottom_right_of", "place_left_of", "place_right_of", "place_top_of",
         "place_bottom_of",
         "place_top_left", "place_top_right", "place_bottom_left", "place_bottom_right",
         "place_left", "place_right", "place_top", "place_bottom"}
#: Commands undone by pressing other keys in the same window.
INVERSE_KEYS = {
    "new_tab": "ctrl+w", "close_tab": "ctrl+shift+t", "reopen_tab": "ctrl+w",
    "next_tab": "ctrl+shift+tab", "previous_tab": "ctrl+tab", "page_down": "pageup",
    "page_up": "pagedown", "back": "alt+right", "forward": "alt+left", "redo": "ctrl+z",
    "undo": "ctrl+y", "desktop": "win+d", "switch_window": "alt+tab",
}
#: Plain "undo" right after one of these undoes it, rather than pressing Ctrl+Z
#: in the app (which would undo some typing instead of the window move).
SMART_UNDO = MOVES | {"switch", "open", "new_tab", "close_tab", "reopen_tab", "next_tab",
                      "previous_tab", "scroll_down", "scroll_up", "show_labels", "desktop",
                      "switch_window"}
SMART_UNDO_SECONDS = 30.0


@dataclass
class UndoStep:
    name: str
    label: str
    run: Callable[[], None]
    at: float = field(default_factory=time.time)


_undo: list[UndoStep] = []


def _focus_hwnd(hwnd: int) -> None:
    from . import window

    if hwnd and not window.activate(window.Target(hwnd, None, "")):
        raise CommandError("that window is gone")


def _target(p: Parsed, before_fg: int) -> int:
    """The window a command acts on: named, aimed at with "in", or in front."""
    # For "to screen N" commands the argument is the screen, not a window.
    arg_is_window = p.command.name in MOVES and not (
        p.command.name == "to_monitor" or p.command.name.endswith("_of"))
    for name in (p.where, p.arg if arg_is_window else ""):
        if name:
            w, score = desktop.find_window(name)
            if w is not None and score >= NAME_ACCEPT:
                return w.hwnd
    return before_fg


def undo_step(p: Parsed, ctx: Ctx, before_fg: int, after_fg: int, target: int, saved,
              result: str) -> UndoStep | None:
    """How to take back a command that just ran, if it can be."""
    name = p.command.name
    label = describe(p)
    if name in MOVES and saved:
        def put_back() -> None:
            if not desktop.set_placement(target, saved):
                raise CommandError("that window is gone")
            _focus_hwnd(target)
        return UndoStep(name, label, put_back)
    if name in ("switch", "open") and before_fg and before_fg != after_fg:
        return UndoStep(name, label, lambda: _focus_hwnd(before_fg))
    if name in INVERSE_KEYS:
        keys = INVERSE_KEYS[name]

        def inverse() -> None:
            if name not in ("desktop", "switch_window"):
                _focus_hwnd(after_fg)
                time.sleep(0.1)
            desktop.press(keys)
        return UndoStep(name, label, inverse)
    if name in ("scroll_down", "scroll_up"):
        notches = 5 if name == "scroll_down" else -5
        return UndoStep(name, label, lambda: desktop.scroll(notches))
    if name in ("type", "paste_again"):
        text = p.arg if name == "type" else ctx.last_any
        n = len(text.replace("\r\n", "\n"))

        def unType() -> None:
            _focus_hwnd(after_fg)
            time.sleep(0.1)
            desktop.press("backspace", n)
        return UndoStep(name, label, unType)
    if name == "scratch" and ctx.last_text and ctx.paste is not None:
        text = ctx.last_text

        def unScratch() -> None:
            if ctx.in_target is not None:
                ctx.in_target(ctx.last_target, lambda: ctx.paste(text))
            else:
                ctx.paste(text)
        return UndoStep(name, label, unScratch)
    if name == "show_labels":
        return UndoStep(name, label, lambda: hide_labels())
    return None


def _undo_command(arg: str, ctx: Ctx) -> str:
    if not _undo:
        raise CommandError("no command to undo")
    step = _undo.pop()
    step.run()
    return f"Undid {step.label}"


def _undo_smart(arg: str, ctx: Ctx) -> str:
    if _undo and _undo[-1].name in SMART_UNDO and time.time() - _undo[-1].at < SMART_UNDO_SECONDS:
        return _undo_command(arg, ctx)
    desktop.press("ctrl+z")
    return "Undo"


def _sleep(arg: str, ctx: Ctx) -> str:
    time.sleep(0.6)  # let the pill and the log catch up
    log.info("commands: putting the PC to sleep")
    desktop.sleep_pc()
    return "Sleeping"


def _type(arg: str, ctx: Ctx) -> str:
    if ctx.paste is None:
        raise CommandError("can't type here")
    ctx.paste(arg)
    return f"Typed {len(arg.split())} words"


def _scroll(notches: int, label: str) -> Callable[[str, Ctx], str]:
    def run(arg: str, ctx: Ctx) -> str:
        desktop.scroll(notches)
        return label
    return run


def _shown(w: desktop.Win, spoken: str = "") -> str:
    """Name for the pill: the program's, if that's what was matched, else
    what was said (found in the title). Store apps live in a frame host
    whose title is the app's name."""
    if w.exe == "applicationframehost":
        return w.title[:30]
    if spoken and desktop.name_score(spoken, w.names()) < NAME_ACCEPT:
        return spoken
    return w.label


def _help(arg: str, ctx: Ctx) -> str:
    return "Say: switch to..., open..., click..., scroll down, new tab, minimize..."


# Patterns: [optional] words, (a|b) choices, and {slot} at the end.
COMMANDS: list[Command] = [
    # Keys, aimed at the window just dictated into when said after a dictation.
    Command("submit", ["submit", "summit", "send it", "send", "press enter", "hit enter",
                       "press into", "press inter", "press center", "hit into",
                       # "press enter" with the second word lost or merged
                       "pressing", "presenter", "press", "presence", "presents",
                       "enter"],
            # A moment for the app to take in a paste first: Ctrl+V is handled
            # asynchronously, and an Enter overtaking it sends nothing.
            _keys("enter", "Enter", delay=0.15), "press Enter", keys=True, risky=True),
    Command("new_line", ["new line", "next line", "line break"], _keys("shift+enter", "New line"),
            "new line without sending (Shift+Enter)", keys=True),
    Command("scratch", ["scratch that", "delete that", "strike that"], _scratch,
            "delete the text LocalFlow just pasted", keys=True),
    Command("tab", ["[press] tab"], _keys("tab", "Tab"), "press Tab", keys=True),
    Command("shift_tab", ["[press] shift tab"], _keys("shift+tab", "Shift+Tab"),
            "press Shift+Tab", keys=True),
    Command("escape", ["[press] escape", "cancel that"], _keys("escape", "Escape"),
            "press Escape", keys=True),
    Command("backspace", ["[press] backspace"], _keys("backspace", "Backspace"),
            "press Backspace", keys=True),
    Command("delete_word", ["delete (word|last word)"], _keys("ctrl+backspace", "Deleted word"),
            "delete the word before the cursor", keys=True),
    Command("undo", ["undo", "undo that"], _undo_smart,
            "undo (Ctrl+Z); right after a window or tab command, undoes that command", keys=True),
    Command("undo_command", ["undo [the|that] last command", "undo [that|the] command",
                             "undo last", "take that back", "put it back", "reverse that"],
            _undo_command, "undo the last voice command, where it can be undone"),
    Command("redo", ["redo"], _keys("ctrl+y", "Redo"), "redo (Ctrl+Y)", keys=True),
    Command("select_all", ["select all"], _keys("ctrl+a", "Selected all"), "select all",
            keys=True),
    Command("copy", ["copy [that]"], _keys("ctrl+c", "Copied"), "copy", keys=True),
    Command("cut", ["cut [that]"], _keys("ctrl+x", "Cut"), "cut", keys=True),
    Command("paste", ["paste [that|it]", "piece", "peace", "paced", "pace"], _paste_keys, "paste",
            keys=True),
    Command("save", ["save [that|this|it|file]"], _keys("ctrl+s", "Saved"), "save (Ctrl+S)",
            keys=True),
    Command("find", ["find"], _keys("ctrl+f", "Find"), "open Find (Ctrl+F)"),
    # Moving around.
    Command("scroll_down", ["scroll down [a bit|a little|more]"], _scroll(-5, "Scrolled down"), "scroll down"),
    Command("scroll_up", ["scroll up [a bit|a little|more]"], _scroll(5, "Scrolled up"), "scroll up"),
    Command("page_down", ["page down"], _keys("pagedown", "Page down"), "page down"),
    Command("page_up", ["page up"], _keys("pageup", "Page up"), "page up"),
    Command("top", ["(go to|jump to) [the] top"], _keys("ctrl+home", "Top"), "go to the top"),
    Command("bottom", ["(go to|jump to) [the] bottom"], _keys("ctrl+end", "Bottom"),
            "go to the bottom"),
    # Tabs and pages.
    Command("new_tab", ["new tab", "open [a] new tab", "[open] [a] new {name} tab"],
            _keys("ctrl+t", "New tab"), "open a new tab", aim=True),
    Command("close_tab", ["close [the|this] tab"], _keys("ctrl+w", "Closed tab"), "close the tab",
            risky=True),
    Command("next_tab", ["next tab"], _keys("ctrl+tab", "Next tab"), "next tab"),
    Command("previous_tab", ["(previous|last) tab"], _keys("ctrl+shift+tab", "Previous tab"),
            "previous tab"),
    Command("reopen_tab", ["reopen [the] tab", "undo close tab"], _keys("ctrl+shift+t",
            "Reopened tab"), "reopen the last closed tab"),
    Command("back", ["go back"], _keys("alt+left", "Back"), "go back a page"),
    Command("forward", ["go forward"], _keys("alt+right", "Forward"), "go forward a page"),
    Command("refresh", ["refresh [the] [page]", "reload [the] [page]"], _keys("f5", "Refreshed"),
            "reload the page"),
    # Windows and apps.
    Command("switch", ["switch to {name}", "go to {name}", "show [me] {name}",
                       "bring up {name}", "focus [on] {name}", "select {name}", "pull up {name}",
                       "(get|take) me to {name}", "activate {name}", "jump to {name}",
                       "bring {name} (up|forward|to the front)", "find {name}"], _switch,
            "bring an open window to the front, by app name or title"),
    Command("open", ["open {name}", "launch {name}", "start {name}"], _open,
            "open an app (or switch to it if it's open), or a website like 'youtube dot com'"),
    Command("minimize", ["minimize [the] [window]", "minimize {name}", "hide {name}"],
            _window_op(desktop.SW_MINIMIZE, "Minimized"), "minimize a window"),
    Command("maximize", ["maximize [the] [window]", "maximize {name}"],
            _window_op(desktop.SW_MAXIMIZE, "Maximized"), "maximize a window"),
    Command("restore", ["restore [the] [window]", "restore {name}"],
            _window_op(desktop.SW_RESTORE, "Restored"), "restore a window"),
    Command("close_window", ["close [the|this] window", "close {name}"],
            _window_op(-1, "Closed"), "close a window (it may ask to save)", risky=True),
    Command("switch_window", ["switch (window|windows)", "last window", "previous window"],
            _keys("alt+tab", "Switched window"), "go to the previous window (Alt+Tab)"),
    Command("center", ["center [the] [window]", "(center|centre) {name}",
                       "(move|put|bring) [the] window to the (center|middle) [of the screen|of my screen|of the monitor|of my monitor]",
                       "(move|put|bring) {name} to the (center|middle) [of the screen|of my screen|of the monitor|of my monitor]"],
            _center, "move a window to the middle of its screen"),
Command("place_top_left", ["(move|put|snap) [it|the window] [to|on] [the] top left [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]",
                       "(move|put|snap) {name} [to|on] [the] top left [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]"],
            _place("top left"), "put a window in the top left quarter of its own monitor"),
    Command("place_top_right", ["(move|put|snap) [it|the window] [to|on] [the] top right [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]",
                       "(move|put|snap) {name} [to|on] [the] top right [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]"],
            _place("top right"), "put a window in the top right quarter of its own monitor"),
    Command("place_bottom_left", ["(move|put|snap) [it|the window] [to|on] [the] bottom left [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]",
                       "(move|put|snap) {name} [to|on] [the] bottom left [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]"],
            _place("bottom left"), "put a window in the bottom left quarter of its own monitor"),
    Command("place_bottom_right", ["(move|put|snap) [it|the window] [to|on] [the] bottom right [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]",
                       "(move|put|snap) {name} [to|on] [the] bottom right [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]"],
            _place("bottom right"), "put a window in the bottom right quarter of its own monitor"),
    Command("place_left", ["(move|put|snap) [it|the window] [to|on] [the] left [half|side] [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]",
                       "(move|put|snap) {name} [to|on] [the] left [half|side] [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]"],
            _place("left"), "put a window in the left half of its own monitor"),
    Command("place_right", ["(move|put|snap) [it|the window] [to|on] [the] right [half|side] [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]",
                       "(move|put|snap) {name} [to|on] [the] right [half|side] [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]"],
            _place("right"), "put a window in the right half of its own monitor"),
    Command("place_top", ["(move|put|snap) [it|the window] [to|on] [the] top half [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]",
                       "(move|put|snap) {name} [to|on] [the] top half [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]"],
            _place("top"), "put a window in the top half of its own monitor"),
    Command("place_bottom", ["(move|put|snap) [it|the window] [to|on] [the] bottom half [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]",
                       "(move|put|snap) {name} [to|on] [the] bottom half [corner] [of the screen|of my screen|of the monitor|of my monitor|of this monitor]"],
            _place("bottom"), "put a window in the bottom half of its own monitor"),
Command("place_top_left_of", ["(move|put|snap) [it|the window] [to|on] [the] top left [corner] (of|on) [the] (monitor|screen|display) [number] {name}",
                       "(move|put|snap) [window] {where} [to|on] [the] top left [corner] (of|on) [the] (monitor|screen|display) [number] {name}"],
            _place("top left", on_monitor=True), "put the window in the top left of monitor N"),
    Command("place_top_right_of", ["(move|put|snap) [it|the window] [to|on] [the] top right [corner] (of|on) [the] (monitor|screen|display) [number] {name}",
                       "(move|put|snap) [window] {where} [to|on] [the] top right [corner] (of|on) [the] (monitor|screen|display) [number] {name}"],
            _place("top right", on_monitor=True), "put the window in the top right of monitor N"),
    Command("place_bottom_left_of", ["(move|put|snap) [it|the window] [to|on] [the] bottom left [corner] (of|on) [the] (monitor|screen|display) [number] {name}",
                       "(move|put|snap) [window] {where} [to|on] [the] bottom left [corner] (of|on) [the] (monitor|screen|display) [number] {name}"],
            _place("bottom left", on_monitor=True), "put the window in the bottom left of monitor N"),
    Command("place_bottom_right_of", ["(move|put|snap) [it|the window] [to|on] [the] bottom right [corner] (of|on) [the] (monitor|screen|display) [number] {name}",
                       "(move|put|snap) [window] {where} [to|on] [the] bottom right [corner] (of|on) [the] (monitor|screen|display) [number] {name}"],
            _place("bottom right", on_monitor=True), "put the window in the bottom right of monitor N"),
    Command("place_left_of", ["(move|put|snap) [it|the window] [to|on] [the] left [half|side] [corner] (of|on) [the] (monitor|screen|display) [number] {name}",
                       "(move|put|snap) [window] {where} [to|on] [the] left [half|side] [corner] (of|on) [the] (monitor|screen|display) [number] {name}"],
            _place("left", on_monitor=True), "put the window in the left of monitor N"),
    Command("place_right_of", ["(move|put|snap) [it|the window] [to|on] [the] right [half|side] [corner] (of|on) [the] (monitor|screen|display) [number] {name}",
                       "(move|put|snap) [window] {where} [to|on] [the] right [half|side] [corner] (of|on) [the] (monitor|screen|display) [number] {name}"],
            _place("right", on_monitor=True), "put the window in the right of monitor N"),
    Command("place_top_of", ["(move|put|snap) [it|the window] [to|on] [the] top half [corner] (of|on) [the] (monitor|screen|display) [number] {name}",
                       "(move|put|snap) [window] {where} [to|on] [the] top half [corner] (of|on) [the] (monitor|screen|display) [number] {name}"],
            _place("top", on_monitor=True), "put the window in the top of monitor N"),
    Command("place_bottom_of", ["(move|put|snap) [it|the window] [to|on] [the] bottom half [corner] (of|on) [the] (monitor|screen|display) [number] {name}",
                       "(move|put|snap) [window] {where} [to|on] [the] bottom half [corner] (of|on) [the] (monitor|screen|display) [number] {name}"],
            _place("bottom", on_monitor=True), "put the window in the bottom of monitor N"),
    Command("to_monitor", ["(move|put|send) [it|this|the window] to (monitor|screen|display) [number] {name}",
                           "(move|put|send) [window] {where} (to|onto|on) [the] (monitor|screen|display) [number] {name}",
                           "window {where} to (monitor|screen|display) [number] {name}",
                           "(monitor|screen|display) [number] {name}"],
            _to_monitor, "move the window in front to monitor N (numbered left to right)"),
    Command("show_monitors", ["show [me] [the] (monitors|screens|displays|monitor numbers|screen numbers)",
                              "which (monitor|screen) is which", "number [the] (monitors|screens)"],
            _show_monitors, "show each monitor's number on it for a few seconds"),
    Command("show_windows", ["show [me] [all] [the] [available|open] windows", "list [the] [open] windows",
                             "(which|what) windows are open", "window list", "show [me] [the] window numbers",
                             "number [the] windows", "show [me] [the] window list"],
            _show_windows, "a numbered list of open windows; then 'switch to 3', 'close 2', 'move 4 to the left'"),
    Command("hide_windows", ["hide [the] (window list|windows list|list)", "close [the] (window list|list)"],
            hide_windows, "hide the window list"),
    Command("next_monitor", ["move [it|the window] to the (next|other) (monitor|screen)",
                             "move [it|the window] (right|to the right) a (monitor|screen)"],
            _keys("win+shift+right", "Moved to the next screen"), "move the window to the next monitor"),
    Command("previous_monitor", ["move [it|the window] to the (previous|left) (monitor|screen)"],
            _keys("win+shift+left", "Moved to the previous screen"), "move the window to the previous monitor"),
    Command("show_labels", ["show [me] [the] [numbered] [click] (labels|numbers) [on screen] [again]",
                            "label [the] (buttons|everything)", "number [the] buttons", "show (clickables|click targets)"],
            _show_labels, "number everything clickable in the window in front; then say 'click <number>'"),
    Command("hide_labels", ["hide [the] [click] (labels|numbers)", "clear [the] (labels|numbers)",
                            "(labels|numbers) off", "stop [the] labels", "no more labels"],
            hide_labels, "take the numbered labels away (they stay up after each click until then)"),
    Command("sleep", ["put [the|my|this] (computer|pc) to sleep", "(sleep|suspend) [the|my|this] (computer|pc)",
                      "go to sleep", "(computer|pc) (sleep|go to sleep)", "sleep mode"],
            _sleep, "put the computer to sleep (asks you to say goodnight first)",
            confirm=["goodnight", "good night", "good nite", "night night"]),
    Command("desktop", ["show [the] desktop"], _keys("win+d", "Desktop"), "show the desktop"),
    # Buttons and text.
    Command("click", ["click [on] [number] {name}", "tap {name}", "click"], _click_kind(),
            "click a button, link, tab or menu item by its name, or a numbered label by its number"),
    Command("double_click", ["double click [on] [number] {name}", "double click"],
            _click_kind(double=True), "double-click a label, a grid cell, or a button by name"),
    Command("right_click", ["right click [on] [number] {name}", "right click"],
            _click_kind(button="right"), "right-click a label, a grid cell, or a button by name"),
    Command("type_into", ["type (in|into) [field|box|label|number] {label} {text}",
                          "type {text} (in|into) [field|box|label|number] {label}",
                          "(fill|fill in) [field|box|label|number] {label} with {text}"],
            _type_into, "type text into a numbered field: 'type into 12 hello'", text_slot=True),
    Command("show_grid", ["show [me] [the] [mouse] grid", "[mouse] grid", "grid mode"],
            _show_grid, "a numbered 3x3 grid over the window: a number zooms in, 'click 5' clicks"),
    Command("hide_grid", ["hide [the] grid", "close [the] grid", "grid off"], hide_grid,
            "take the grid away"),
    Command("type", ["type {text}"], _type, "type some text", text_slot=True),
    Command("paste_again", ["paste [that|it] again", "repeat [that|the] [last] dictation",
                            "paste [the] last dictation"], _paste_again,
            "paste the last dictation again where the cursor is"),
    Command("history", ["(show|open) [my|the] [dictation] history", "dictation history"],
            _history, "open the dictation history window"),
    Command("help", ["what can i say", "help", "show commands"], _help, "list commands"),
]
BY_NAME = {c.name: c for c in COMMANDS}

#: Said in command mode to leave it (the stop phrase works too).
EXIT = ["exit command mode", "exit commands", "stop commands", "leave command mode",
        "exit", "done", "that is all"]
#: Said after the start phrase (or after a dictation) to enter command mode.
ENTER = ["command mode", "commands", "take commands", "control mode", "switch mode",
         "back to commands", "command mode please"]
#: Said in command mode to dictate (and come back to command mode after).
DICTATE = ["dictation mode", "dictate", "start dictating", "switch mode", "take dictation",
           "text mode", "typing mode", "switch to dictation", "dictation"]
#: Answers to "say yes to ..." for a risky plan.
YES = ["yes", "yes please", "yeah", "do it", "confirm", "go ahead", "okay do it"]
NO = ["no", "cancel", "do not", "stop", "never mind", "nope"]


@dataclass
class Pattern:
    """A spoken pattern compiled to groups, matched without expanding it.

    Each group is a list of alternatives (word lists); [] among them means
    the group is optional. A str is a named slot of one or more words:
    {name} or {text} (the command's argument), {where} (the window to do it
    in: "move window 8 to screen 1"), {label} (a numbered label: "type into
    12 ..."). Writing every combination out instead gave 42,000 forms, and
    checking one long burst of speech against them took a minute - the
    listener fell that far behind and missed stop phrases.
    """
    command: Command
    groups: list[list[list[str]] | str]

    @staticmethod
    def compile(command: Command, text: str) -> Pattern:
        groups: list[list[list[str]] | str] = []
        for part in re.findall(r"\{\w+\}|\[[^\]]*\]|\([^)]*\)|[^\s\[\]()]+", text):
            if part.startswith("{"):
                groups.append(part[1:-1])
            elif part.startswith("["):
                groups.append([[]] + [normalise(a) for a in part[1:-1].split("|")])
            elif part.startswith("("):
                groups.append([normalise(a) for a in part[1:-1].split("|")])
            else:
                groups.append([normalise(part)])
        return Pattern(command, groups)

    def match(self, words: list[str]) -> tuple[float, dict[str, list[str]]] | None:
        """(score, {slot: words}) for the best reading of all of ``words``,
        or None. Score = the worst word's similarity; ties go to the shortest
        slots, so "move chrome to the left" names "chrome"."""
        n, groups = len(words), self.groups
        memo: dict[tuple[int, int], tuple | None] = {}

        def go(i: int, g: int):
            # -> (score, -slot words, ((slot, start, end), ...)) for words[i:] vs groups[g:]
            if g == len(groups):
                return (1.0, 0, ()) if i == n else None
            key = (i, g)
            if key in memo:
                return memo[key]
            result = None
            group = groups[g]
            if isinstance(group, str):  # a slot: one or more words
                for j in range(i + 1, n + 1):
                    rest = go(j, g + 1)
                    if rest is not None:
                        cand = (rest[0], rest[1] - (j - i), ((group, i, j),) + rest[2])
                        if result is None or cand[:2] > result[:2]:
                            result = cand
            else:
                for alt in group:
                    k = len(alt)
                    if i + k > n:
                        continue
                    score = 1.0
                    for x, y in zip(words[i : i + k], alt):
                        score = min(score, _word_sim(x, y))
                        if score < ACCEPT:
                            break
                    if score < ACCEPT:
                        continue
                    rest = go(i + k, g + 1)
                    if rest is not None:
                        cand = (min(score, rest[0]), rest[1], rest[2])
                        if result is None or cand[:2] > result[:2]:
                            result = cand
            memo[key] = result
            return result

        r = go(0, 0)
        if r is None:
            return None
        return r[0], {name: words[i:j] for name, i, j in r[2]}


@functools.lru_cache(maxsize=65536)
def _word_sim(a: str, b: str) -> float:
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


#: Said after a command to aim it at a window: "new tab in Chrome".
_WHERE = ("in", "on")
#: Dropped from the end of a name: "the Chrome Twitch window" -> "chrome twitch".
_NAME_TAIL = ("window", "app", "application", "program", "tab")


def _clean_name(words: list[str]) -> list[str]:
    """A spoken name as the words to look up.

    Spelled out ("n o t i o n") wins: letter by letter is what people do when
    the word itself keeps being misheard, so it's the most exact form. A
    name said and then spelled ("no shun n o t i o n") keeps only the spelling.
    """
    words = list(words)
    spelled, run = [], []
    for w in words + [""]:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if len(run) >= 3:
            spelled.append("".join(run))
        run = []
    if spelled:
        return [spelled[-1]]
    while len(words) > 1 and words[0] in ("the", "my", "a", "that", "this", "me", "window",
                                          "number"):
        words = words[1:]
    if len(words) > 1 and words[-1] in _NAME_TAIL:
        words = words[:-1]
    return words


#: Said to accept "learn this?" after a missed command.
REMEMBER = ["remember that", "remember it", "remember this", "learn that", "learn it",
            "save that", "yes remember that"]
#: A learned phrase also matches what's heard this close to it, whole.
LEARNED_MATCH = 0.9


def load_learned(path: Path | None) -> dict[str, dict]:
    """Phrases learned from misses: {"select no shun": {"command": "switch",
    "arg": "notion", "where": ""}}. Kept on this PC only (learned.json)."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("command") in BY_NAME}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}


@dataclass
class Grammar:
    """Spoken patterns -> commands. Extra spellings come from the config;
    phrases learned from misses come from ``learned_file`` (None: none)."""
    extra: dict[str, list[str]] = field(default_factory=dict)
    learned_file: Path | None = None

    def __post_init__(self) -> None:
        self.patterns = [Pattern.compile(c, p) for c in COMMANDS
                         for p in c.patterns + self.extra.get(c.name, [])]
        self.learned = load_learned(self.learned_file)
        self.remember = [normalise(s) for s in REMEMBER]
        self.enter = [normalise(s) for s in ENTER]
        self.exit = [normalise(s) for s in EXIT]
        self.yes = [normalise(s) for s in YES]
        self.dictate = [normalise(s) for s in DICTATE]
        self.no = [normalise(s) for s in NO]

    def parse(self, words: list[str]) -> Parsed | None:
        """The command these words are, if they are one in full.

        The whole utterance has to be the command, give or take a lead-in
        ("okay", "please") and a slot's free words: "switch to Chrome" is a
        command, "I'll switch to Chrome later" is not. A command can be aimed
        at a window by ending it with "in <name>": "open a new tab in Chrome"
        switches to Chrome first. The plain reading wins unless the split one
        is a better fit and its window exists.
        """
        words = trim(words)
        if not words:
            return None
        known = self._learned(words)
        if known is not None:
            return known
        best = self._parse(words)
        for i in range(len(words) - 2, 0, -1):
            if words[i] not in _WHERE:
                continue
            head, tail = words[:i], _clean_name(words[i + 1 :])
            if not tail or len(tail) > 4:
                continue  # a window's name is a few words, not the rest of a sentence
            p = self._parse(head)
            if p is None or p.command.name in ("switch", "open", "type"):
                continue
            if best is not None and best.score >= p.score and resolvable(best):
                continue
            w, score = desktop.find_window(" ".join(tail))
            if w is not None and score >= NAME_ACCEPT:
                p.where = " ".join(tail)
                best = p
                break
        return best

    # -- learned from misses ------------------------------------------------

    def _learned(self, words: list[str]) -> Parsed | None:
        if not self.learned:
            return None
        said = " ".join(words)
        best, best_score = None, LEARNED_MATCH
        for phrase, d in self.learned.items():
            score = 1.0 if phrase == said else _word_sim(phrase, said)
            if score >= best_score:
                best, best_score = d, score
        if best is None:
            return None
        return Parsed(BY_NAME[best["command"]], best.get("arg", ""), best_score,
                      where=best.get("where", ""))

    def learn(self, phrase: str, p: Parsed) -> str:
        """Remember ``phrase`` as another way to say ``p``. Returns the key."""
        key = " ".join(trim(normalise(phrase)))
        self.learned[key] = {"command": p.command.name, "arg": p.arg, "where": p.where}
        self._save_learned()
        return key

    def forget(self, phrase: str) -> None:
        self.learned.pop(phrase, None)
        self._save_learned()

    def _save_learned(self) -> None:
        if self.learned_file is None:
            return
        try:
            self.learned_file.write_text(json.dumps(self.learned, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not save learned phrases: %s", exc)

    def _parse(self, words: list[str]) -> Parsed | None:
        # Commands are short; only "type ..." carries free text of any length.
        if len(words) > MAX_COMMAND_WORDS and not (words and _word_sim(words[0], "type") >= ACCEPT):
            return None
        best: Parsed | None = None
        for pat in self.patterns:
            m = pat.match(words)
            if m is None:
                continue
            score, slots = m
            c = pat.command
            words_ = slots.get("name") or slots.get("text") or []
            arg = " ".join(words_ if c.text_slot else _clean_name(words_))
            where = " ".join(_clean_name(slots.get("where", [])))
            label = " ".join(slots.get("label", []))
            size = len(arg.split()) + len(where.split()) + len(label.split())
            if score >= ACCEPT and (best is None or score > best.score
                                    or (score == best.score and size < best.size)):
                if c.aim and arg and not where:
                    arg, where = "", arg
                best = Parsed(c, arg, score, where=where, label=label)
        return best

    def is_one(self, words: list[str], forms: list[list[str]]) -> bool:
        words = trim(words)
        return bool(words) and any(
            abs(len(words) - len(f)) <= 1 and _aligned(words, f) >= ACCEPT for f in forms)


def trim(words: list[str]) -> list[str]:
    words = list(words)
    while words and words[0] in LEAD:
        words.pop(0)
    while len(words) > 1 and words[-1] in TRAIL:
        words.pop()
    return words


def is_noise(words: list[str]) -> bool:
    return not words or " ".join(words) in NOISE


def resolvable(p: Parsed) -> bool:
    """Whether a command's target exists, without doing anything: lets a
    dictation that merely starts like a command ("open the file and...")
    stay a dictation."""
    name = p.command.name
    if (name in ("switch", "minimize", "maximize", "restore", "close_window", "center")
            or name.startswith("place_")) and p.arg:
        _, score = desktop.find_window(p.arg)
        return score >= NAME_ACCEPT
    if name == "open":
        _, a = desktop.find_app(p.arg)
        _, w = desktop.find_window(p.arg)
        return max(a, w) >= NAME_ACCEPT
    return name not in ("click", "type")


def describe(p: Parsed) -> str:
    """For the pill: "close window chrome", "type I'm running late"."""
    return f"{p.command.name.replace('_', ' ')} {p.arg}".strip()


# -- the LLM layer -----------------------------------------------------------

_PROMPT = """You turn a spoken request into steps for a Windows voice-control tool.
Reply with JSON only, in this shape: {{"steps": [{{"command": "<name>", "arg": "<text>"}}]}}
Use only these commands (arg is "" unless the command takes one):
{commands}
Windows open now (program: title):
{windows}
Rules: at most 4 steps. Use the exact command names above. For "switch", "open",
"minimize", "maximize", "restore", "close_window" the arg is an app name or a
window title from the list. For "click" the arg is the visible name of a
button. For "type" the arg is the exact text to type. To aim keys at a
window, switch to it first. If the request can't be done with these
commands, reply {{"steps": []}}.
Examples:
"open a new tab in chrome" -> {{"steps": [{{"command": "switch", "arg": "chrome"}}, {{"command": "new_tab", "arg": ""}}]}}
"put the twitch window in the middle" -> {{"steps": [{{"command": "center", "arg": "twitch"}}]}}
"get rid of spotify" -> {{"steps": [{{"command": "minimize", "arg": "spotify"}}]}}
"what's the time in tokyo" -> {{"steps": []}}"""


def _describe() -> str:
    lines = []
    for c in COMMANDS:
        if c.name == "help":
            continue
        takes = "arg: text" if c.text_slot else ("arg: name" if any("{" in p for p in c.patterns)
                                                 else "")
        lines.append(f"- {c.name}: {c.help}" + (f" ({takes})" if takes else ""))
    return "\n".join(lines)


def plan_with_llm(client, model: str, request: str, timeout: float = 30.0,
                  keep_alive: str | int = "30m") -> list[Parsed]:
    """Steps for a request the patterns didn't cover, chosen by the LLM from
    COMMANDS only. Anything unknown or malformed is dropped, not guessed at."""
    wins = desktop.windows()[:20]
    window_list = "\n".join(f"- {w.exe}: {w.title[:60]}" for w in wins) or "- (none)"
    resp = client.post(
        "/api/chat",
        json={
            "model": model, "stream": False, "format": "json", "think": False,
            # The cleanup model's own keep-alive: the default would unload
            # it after 5 minutes and slow the next dictation's cleanup.
            "keep_alive": keep_alive,
            "options": {"temperature": 0.0, "num_predict": 200},
            "messages": [
                {"role": "system", "content": _PROMPT.format(commands=_describe(),
                                                             windows=window_list)},
                {"role": "user", "content": request},
            ],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    content = resp.json().get("message", {}).get("content", "")
    try:
        steps = json.loads(content).get("steps", [])
    except (ValueError, AttributeError):
        log.info("commands: LLM reply wasn't JSON")
        return []
    out = []
    for step in steps[:4] if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        c = BY_NAME.get(str(step.get("command", "")))
        arg = str(step.get("arg", "") or "").strip()
        # It sometimes copies the window list's "program: title" form.
        if c is not None and c.name != "type" and ": " in arg:
            arg = arg.split(": ", 1)[1]
        takes_arg = c is not None and any("{" in p for p in c.patterns)
        if c is None or c.name == "help" or (takes_arg and not arg and c.name in
                                              ("switch", "open", "click", "type")):
            log.info("commands: LLM step %r dropped", step.get("command"))
            continue
        out.append(Parsed(c, arg if takes_arg else "", 1.0))
    return out


# -- running them ------------------------------------------------------------

class Runner:
    """One worker thread runs every command, in order (COM likes one thread)."""

    def __init__(self, on_done: Callable[[Parsed, str, bool], None]):
        self.on_done = on_done
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def submit(self, p: Parsed, ctx: Ctx, wrap: Callable[[Callable[[], None]], None] | None = None
               ) -> None:
        """Run ``p`` soon. ``wrap`` runs it inside something (the target window)."""
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="localflow-commands")
            self._thread.start()
        self._jobs.put((p, ctx, wrap))

    def _loop(self) -> None:
        desktop.per_monitor_dpi()
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        while True:
            p, ctx, wrap = self._jobs.get()
            result: list[str] = []
            undo: list[UndoStep | None] = []
            try:
                def go() -> None:
                    name = p.command.name
                    before_fg = desktop.foreground()
                    target = _target(p, before_fg) if name in MOVES else 0
                    saved = desktop.get_placement(target) if target else None
                    if p.where:
                        _switch(p.where, ctx)
                        time.sleep(0.2)  # let it take focus before the keys
                    if p.label:
                        ctx.label = p.label
                    result.append(p.command.action(p.arg, ctx))
                    if name not in ("undo_command", "undo") or not result[-1].startswith("Undid"):
                        if name not in ("undo_command",):
                            undo.append(undo_step(p, ctx, before_fg, desktop.foreground(), target,
                                                  saved, result[-1]))

                if wrap is not None:
                    wrap(go)
                else:
                    go()
                if undo and undo[0] is not None:
                    _undo.append(undo[0])
                    del _undo[:-20]
                log.info("commands: %s done", p.command.name)
                self.on_done(p, result[0] if result else "", True)
            except CommandError as exc:
                log.info("commands: %s not done: %s", p.command.name, exc)
                self.on_done(p, str(exc)[:1].upper() + str(exc)[1:], False)
            except Exception as exc:
                log.warning("commands: %s failed: %s", p.command.name, exc)
                self.on_done(p, f"{p.command.name} failed", False)
