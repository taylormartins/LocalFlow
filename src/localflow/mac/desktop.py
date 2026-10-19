"""Command mode's hands on a Mac: keys, the mouse, apps, windows (to list and
switch to), screens. Replaces the Windows functions of the same names in
desktop.py.

Moving, resizing, minimising and closing other apps' windows, and numbering
what's clickable, need macOS Accessibility (AXUIElement); that's the next
step, and until then those raise NotImplementedError, which command mode
shows as "not on the Mac yet".

A window's "hwnd" here is (process ID << 24) | window number, so the number
alone is enough to bring its app back (window.activate decodes it).
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from ..platform import wintypes

log = logging.getLogger(__name__)

__all__ = [
    "press", "scroll", "click_at", "windows", "foreground", "focus", "show", "close",
    "monitors", "monitor_of", "to_monitor", "apps", "launch", "open_url", "sleep_pc",
    "window_rect", "work_area", "caret_point", "controls", "activate_control", "place",
    "center", "arrange", "move_windows", "swap_windows", "get_placement", "set_placement",
    "snapshot", "restore", "window_chips", "confirm_terminal_paste", "per_monitor_dpi",
    "_exe_path", "pid_of", "encode",
]

#: Windows shortcuts the commands use -> the Mac's.
MAC_CHORDS = {
    "ctrl+c": "cmd+c", "ctrl+v": "cmd+v", "ctrl+x": "cmd+x", "ctrl+a": "cmd+a",
    "ctrl+z": "cmd+z", "ctrl+y": "cmd+shift+z", "ctrl+s": "cmd+s", "ctrl+f": "cmd+f",
    "ctrl+t": "cmd+t", "ctrl+w": "cmd+w", "ctrl+shift+t": "cmd+shift+t",
    "ctrl+shift+c": "cmd+c", "ctrl+shift+v": "cmd+v",
    "alt+left": "cmd+[", "alt+right": "cmd+]", "f5": "cmd+r",
    "ctrl+home": "cmd+up", "ctrl+end": "cmd+down", "ctrl+backspace": "alt+backspace",
    "win+d": "f11", "alt+tab": "cmd+tab", "alt+f4": "cmd+q",
}
NOT_YET = "isn't on the Mac yet (window control comes next)"


def encode(pid: int, number: int) -> int:
    return (int(pid) << 24) | (int(number) & 0xFFFFFF)


def pid_of(hwnd: int) -> int:
    return int(hwnd) >> 24


def per_monitor_dpi() -> None:
    """Nothing to do: macOS coordinates are points everywhere."""


def confirm_terminal_paste(timeout: float = 1.0) -> bool:
    return False  # a Windows Terminal dialog


# -- keys and the mouse ------------------------------------------------------

_mouse = None


def press(chord: str, times: int = 1) -> None:
    """Press a chord named the Windows way ("ctrl+shift+tab"); Mac shortcuts
    are used where they differ (Ctrl+C is Cmd+C). By key code (mac/keys.py)."""
    from .keys import press as press_codes

    chord = MAC_CHORDS.get(chord, chord).replace("win", "cmd")
    press_codes(chord, times)


def scroll(notches: int) -> None:
    global _mouse
    if _mouse is None:
        from pynput.mouse import Controller

        _mouse = Controller()
    _mouse.scroll(0, notches)


def click_at(x: int, y: int, button: str = "left", double: bool = False) -> None:
    global _mouse
    from pynput.mouse import Button, Controller

    if _mouse is None:
        _mouse = Controller()
    _mouse.position = (x, y)
    _mouse.click(Button.right if button == "right" else Button.left, 2 if double else 1)


# -- windows (listing and switching) --------------------------------------------

_bounds: dict[int, tuple[int, int, int, int]] = {}
#: Not windows you'd want to switch to.
_SKIP = {"Window Server", "Dock", "Control Center", "Notification Center", "SystemUIServer",
         "Spotlight", "WindowManager"}


def _window_list(number: int | None = None) -> list[dict]:
    import Quartz

    if number is None:
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        return list(Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or [])
    return list(Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow,
                                                  number) or [])


def windows():
    """App windows on screen, front to back. Titles need Screen Recording
    permission; without it the app's name stands in."""
    import os

    from ..desktop import Win

    me = os.getpid()
    out = []
    for w in _window_list():
        owner = str(w.get("kCGWindowOwnerName") or "")
        pid = int(w.get("kCGWindowOwnerPID") or 0)
        b = w.get("kCGWindowBounds") or {}
        width, height = int(b.get("Width", 0)), int(b.get("Height", 0))
        if (w.get("kCGWindowLayer", 0) != 0 or pid == me or owner in _SKIP
                or width < 80 or height < 40):
            continue
        hwnd = encode(pid, int(w.get("kCGWindowNumber", 0)))
        x, y = int(b.get("X", 0)), int(b.get("Y", 0))
        _bounds[hwnd] = (x, y, x + width, y + height)
        title = str(w.get("kCGWindowName") or "") or owner
        out.append(Win(hwnd, title, owner.lower().replace(" ", ""), owner))
    return out


def foreground() -> int:
    """The front app's front window."""
    from .window import frontmost

    app = frontmost()
    if app is None:
        return 0
    pid = int(app.processIdentifier())
    for w in windows():
        if pid_of(w.hwnd) == pid:
            return w.hwnd
    return encode(pid, 0)


def focus(w) -> bool:
    from .window import Target, activate

    return activate(Target(w.hwnd, None, w.label))


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    if hwnd not in _bounds:
        windows()
    return _bounds.get(hwnd, (0, 0, 0, 0))


def _exe_path(hwnd: int) -> str:
    from AppKit import NSRunningApplication

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid_of(hwnd))
    url = app.bundleURL() if app is not None else None
    return str(url.path()) if url is not None else ""


def window_chips(wins):
    """The number chips of "show windows": every listed window's top left."""
    chips = []
    for i, w in enumerate(wins):
        left, top, right, bottom = window_rect(w.hwnd)
        if right - left < 80:
            continue
        chips.append((i + 1, (left + 8, top + 6, left + 64, top + 46)))
    return chips


# -- screens -------------------------------------------------------------------

def monitors():
    """(index, whole screen, usable area) per screen, numbered left to right."""
    from .overlay import screens

    found = sorted(screens(), key=lambda s: (s[0].left, s[0].top))
    return [(i, whole, usable) for i, (whole, usable) in enumerate(found)]


def monitor_of(hwnd: int, mons=None) -> int:
    mons = mons or monitors()
    left, top, right, bottom = window_rect(hwnd)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    for i, (_h, whole, _u) in enumerate(mons):
        if whole.left <= cx < whole.right and whole.top <= cy < whole.bottom:
            return i + 1
    return 0


def work_area(hwnd: int) -> wintypes.RECT:
    mons = monitors()
    n = monitor_of(hwnd, mons)
    return mons[n - 1][2] if 1 <= n <= len(mons) else mons[0][2]


# -- apps ------------------------------------------------------------------------

_APP_DIRS = ["/Applications", "/Applications/Utilities", "/System/Applications",
             "/System/Applications/Utilities", str(Path.home() / "Applications")]
_apps: list = []
_apps_at = 0.0


def apps(max_age: float = 300.0):
    """Installed apps (the .app bundles in the usual folders)."""
    global _apps, _apps_at
    from ..desktop import App

    if _apps and time.monotonic() - _apps_at < max_age:
        return _apps
    out = []
    for folder in _APP_DIRS:
        try:
            for p in sorted(Path(folder).glob("*.app")):
                out.append(App(p.stem, str(p)))
        except OSError:
            continue
    _apps, _apps_at = out, time.monotonic()
    return out


def launch(app) -> None:
    subprocess.Popen(["open", "-a", app.parsing_name])


def open_url(url: str) -> None:
    subprocess.Popen(["open", url])


def sleep_pc() -> None:
    subprocess.run(["pmset", "sleepnow"], check=True)


def caret_point():
    """Where the text cursor is: not known yet on the Mac (it needs
    Accessibility), so no paste effect there for now."""
    return None


# -- window control: next step (Accessibility) -------------------------------------

def _not_yet(*_args, **_kwargs):
    raise NotImplementedError(NOT_YET)


show = close = to_monitor = place = center = arrange = move_windows = _not_yet
swap_windows = get_placement = set_placement = snapshot = restore = _not_yet
controls = activate_control = _not_yet
