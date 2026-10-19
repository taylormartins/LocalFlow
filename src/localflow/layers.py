"""The overlay's windows (the pill, labels, lists, the command sheet, effects):
see-through, click-through, never focused, always on top. Windows and Mac.

On Windows each is a Tk toplevel with extended styles (layered,
transparent to clicks, tool window, no-activate, topmost), shown and moved
with SetWindowPos. On a Mac each is a borderless Tk window with a
transparent background whose NSWindow is told to ignore the mouse, float
above other apps and appear on every Space; it is moved with Tk's geometry
and shown with orderFrontRegardless (which doesn't activate the app).

A "handle" is the HWND on Windows and the Tk window on a Mac.
"""

from __future__ import annotations

import ctypes
import logging

from .platform import IS_MAC, dll, wintypes

log = logging.getLogger(__name__)

#: The colour that becomes see-through (Windows), or Tk's transparent colour
#: (Mac). On Windows Tk doesn't antialias, so no edge pixels blend with the
#: key and the pill's rounded ends have no fringe.
KEY = "systemTransparent" if IS_MAC else "#010203"

user32 = dll("user32")
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
user32.IsWindowVisible.argtypes = [wintypes.HWND]
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
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SW_HIDE = 0
MONITOR_DEFAULTTONEAREST = 2
#: Per-monitor-v2. Set on the overlay thread only, so the rest of the process
#: (pystray, hotkeys) keeps the DPI behaviour it was written against.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def scale() -> float:
    """Pixels per point for drawing. Windows: the system DPI (per-monitor
    aware on the Tk thread). Mac: 1, since Tk draws in points and Retina
    screens double them itself."""
    if IS_MAC:
        return 1.0
    try:
        user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        return user32.GetDpiForSystem() / 96
    except (AttributeError, OSError):
        return 1.0


def transparent(win) -> None:
    """The window's background becomes see-through."""
    if IS_MAC:
        win.configure(bg=KEY)
        try:
            win.wm_attributes("-transparent", True)
        except Exception:
            log.debug("no -transparent on this Tk", exc_info=True)
        return
    win.configure(bg=KEY)
    win.attributes("-transparentcolor", KEY)


def prepare(win):
    """Make a mapped Tk window click-through, never focused, on top, and
    hidden. Returns its handle."""
    if IS_MAC:
        from .mac import overlay as mac

        mac.prepare(win)
        return win
    hwnd = user32.GetParent(win.winfo_id())
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(
        hwnd, GWL_EXSTYLE,
        ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
    )
    user32.ShowWindow(hwnd, SW_HIDE)
    return hwnd


def show(handle, x: int, y: int, w: int | None = None, h: int | None = None) -> bool:
    """Show it at x, y (and size w x h if given), on top, without focus."""
    if IS_MAC:
        from .mac import overlay as mac

        return mac.show(handle, x, y, w, h)
    flags = SWP_NOACTIVATE | SWP_SHOWWINDOW | (SWP_NOSIZE if w is None else 0)
    ok = bool(user32.SetWindowPos(handle, HWND_TOPMOST, int(x), int(y), int(w or 0), int(h or 0),
                                  flags))
    if not ok:
        log.warning("overlay SetWindowPos failed (error %d)", ctypes.get_last_error())
    return ok


def hide(handle) -> None:
    if IS_MAC:
        from .mac import overlay as mac

        mac.hide(handle)
        return
    user32.ShowWindow(handle, SW_HIDE)


def visible(handle) -> bool:
    if IS_MAC:
        from .mac import overlay as mac

        return mac.visible(handle)
    return bool(user32.IsWindowVisible(handle))


def raise_(handle) -> None:
    """Back on top of the other overlay windows (without moving it)."""
    if IS_MAC:
        from .mac import overlay as mac

        mac.raise_(handle)
        return
    user32.SetWindowPos(handle, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOMOVE)


def work_area() -> wintypes.RECT:
    """The usable area (no taskbar / menu bar or Dock) of the screen the
    window in front is on, in screen pixels (Windows) or points (Mac)."""
    if IS_MAC:
        from .mac import overlay as mac

        return mac.work_area()
    monitor = user32.MonitorFromWindow(user32.GetForegroundWindow(), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    return info.rcWork
