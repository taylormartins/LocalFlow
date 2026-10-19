"""The overlay's windows on a Mac (layers.py calls these).

Tk makes each a borderless window; its NSWindow is then told to ignore the
mouse (click-through), float above normal windows and full-screen apps, and
join every Space. orderFrontRegardless shows it without activating Golden
Flow, so the app you're typing in keeps focus.

Coordinates are points from the top left of the main screen, the same as
Tk's geometry; NSScreen's are from the bottom left, so they're flipped here.
"""

from __future__ import annotations

import logging

from ..platform import wintypes

log = logging.getLogger(__name__)

#: NSStatusWindowLevel: above normal and floating windows.
LEVEL = 25
#: canJoinAllSpaces | stationary | ignoresCycle | fullScreenAuxiliary
BEHAVIOUR = (1 << 0) | (1 << 4) | (1 << 6) | (1 << 8)


def _ns(win):
    """The NSWindow behind a Tk window (found by a title only it has)."""
    ns = getattr(win, "_lf_ns", None)
    if ns is not None:
        return ns
    from AppKit import NSApplication

    title = f"localflow-layer-{id(win)}"
    win.title(title)
    win.update_idletasks()
    for w in NSApplication.sharedApplication().windows():
        if str(w.title()) == title:
            win._lf_ns = w
            return w
    raise RuntimeError("could not find the overlay's NSWindow")


def prepare(win) -> None:
    from AppKit import NSColor

    ns = _ns(win)
    ns.setIgnoresMouseEvents_(True)
    ns.setLevel_(LEVEL)
    ns.setCollectionBehavior_(BEHAVIOUR)
    ns.setHasShadow_(False)
    ns.setOpaque_(False)
    ns.setBackgroundColor_(NSColor.clearColor())
    ns.orderOut_(None)


def show(win, x: int, y: int, w: int | None = None, h: int | None = None) -> bool:
    if w is None:
        win.geometry(f"+{int(x)}+{int(y)}")
    else:
        win.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")
    win.update_idletasks()
    _ns(win).orderFrontRegardless()
    return True


def hide(win) -> None:
    _ns(win).orderOut_(None)


def visible(win) -> bool:
    return bool(_ns(win).isVisible())


def raise_(win) -> None:
    _ns(win).orderFrontRegardless()


def screens() -> list[tuple[wintypes.RECT, wintypes.RECT]]:
    """(whole screen, usable area without menu bar and Dock) per screen, in
    top-left points, main screen first."""
    from AppKit import NSScreen

    all_ = list(NSScreen.screens())
    if not all_:
        return []
    top = all_[0].frame().size.height

    def rect(fr) -> wintypes.RECT:
        left = int(fr.origin.x)
        y = int(top - (fr.origin.y + fr.size.height))
        return wintypes.RECT(left, y, left + int(fr.size.width), y + int(fr.size.height))

    return [(rect(s.frame()), rect(s.visibleFrame())) for s in all_]


def work_area() -> wintypes.RECT:
    """The usable area of the screen the mouse is on (where you're working)."""
    from AppKit import NSEvent

    found = screens()
    if not found:
        return wintypes.RECT(0, 0, 1440, 900)
    p = NSEvent.mouseLocation()
    from AppKit import NSScreen

    top = NSScreen.screens()[0].frame().size.height
    mx, my = int(p.x), int(top - p.y)
    for whole, usable in found:
        if whole.left <= mx < whole.right and whole.top <= my < whole.bottom:
            return usable
    return found[0][1]
