"""Remember the app you started in and bring it back (paste where I started),
on a Mac. Windows' window.py tracks a window handle; here it's the app's
process ID: macOS activates apps, and an app brings back its own key window.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: NSApplicationActivateIgnoringOtherApps
ACTIVATE = 1 << 1


@dataclass
class Target:
    hwnd: int  # the app's process ID (named for the Windows side)
    focus: int | None
    title: str  # the app's name, for the log

    def __str__(self) -> str:
        return self.title or str(self.hwnd)


def _pid(hwnd: int) -> int:
    """A process ID, or command mode's window number (pid << 24 | number)."""
    return int(hwnd) >> 24 if int(hwnd) >= 1 << 24 else int(hwnd)


def _app(hwnd: int):
    from AppKit import NSRunningApplication

    return NSRunningApplication.runningApplicationWithProcessIdentifier_(_pid(hwnd))


def frontmost():
    from AppKit import NSWorkspace

    return NSWorkspace.sharedWorkspace().frontmostApplication()


def current() -> Target | None:
    app = frontmost()
    if app is None:
        return None
    return Target(int(app.processIdentifier()), None, str(app.localizedName() or ""))


def alive(t: Target | None) -> bool:
    if not t:
        return False
    app = _app(t.hwnd)
    return app is not None and not app.isTerminated()


def activate(t: Target, timeout: float = 0.6) -> bool:
    """Bring the app to the front. True if it got there."""
    if not alive(t):
        return False
    app = _app(t.hwnd)
    if app.isHidden():
        app.unhide()
    app.activateWithOptions_(ACTIVATE)
    deadline = time.monotonic() + timeout
    while True:
        front = frontmost()
        if front is not None and int(front.processIdentifier()) == _pid(t.hwnd):
            return True
        if time.monotonic() > deadline:
            log.warning("could not bring %s to the front", t)
            return False
        time.sleep(0.02)
