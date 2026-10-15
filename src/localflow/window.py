"""Remember a window and bring it back: "paste where I started".

With inject.paste_to = "start", the window (and the control inside it) that
had focus when the dictation began is the one that gets the text, however much
you click around while talking. The text is pasted there, a voice command like
"submit" runs there too, and you're returned to wherever you'd got to.

Windows only lets an app bring a window to the front if it received the last
input event. LocalFlow taps an unassigned key (marked as its own, so its
keyboard hook ignores it) to qualify, falls back to attaching to the
foreground thread's input, and always checks the switch actually happened:
pasting into the wrong window is worse than not pasting.
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetFocus.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]

SW_RESTORE = 9


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]


@dataclass(frozen=True)
class Target:
    hwnd: int
    focus: int | None  # the control inside it that had keyboard focus
    title: str

    def __str__(self) -> str:
        # No title: they can be personal (a document name, a chat, a tab).
        return f"window {self.hwnd:#x}"


def _thread_of(hwnd: int) -> int:
    return user32.GetWindowThreadProcessId(hwnd, None)


def _title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def current() -> Target | None:
    """The window with focus right now, and its focused control."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    focus = None
    if user32.GetGUIThreadInfo(_thread_of(hwnd), ctypes.byref(info)):
        focus = info.hwndFocus or None
    return Target(int(hwnd), int(focus) if focus else None, _title(hwnd))


def alive(t: Target | None) -> bool:
    return bool(t and user32.IsWindow(t.hwnd))


def activate(t: Target, timeout: float = 0.6) -> bool:
    """Bring ``t`` to the front with its control focused. True if it worked."""
    if not alive(t):
        return False
    if user32.IsIconic(t.hwnd):
        user32.ShowWindow(t.hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() != t.hwnd:
        from .hotkey import VK_MASK, _tap

        _tap(VK_MASK)  # "we sent the last input": allowed to switch windows
        user32.SetForegroundWindow(t.hwnd)
    if user32.GetForegroundWindow() != t.hwnd:
        # Fallback: share input state with the current foreground thread.
        me = kernel32.GetCurrentThreadId()
        fg = _thread_of(user32.GetForegroundWindow())
        attached = fg and fg != me and user32.AttachThreadInput(me, fg, True)
        try:
            user32.BringWindowToTop(t.hwnd)
            user32.SetForegroundWindow(t.hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(me, fg, False)
    deadline = time.monotonic() + timeout
    while user32.GetForegroundWindow() != t.hwnd:
        if time.monotonic() > deadline:
            log.warning("could not bring %s to the front", t)
            return False
        time.sleep(0.02)
    _refocus(t)
    return True


def _refocus(t: Target) -> None:
    """Put keyboard focus back on the control that had it (a native text box
    in a dialog, say). Browsers and terminals are one control: nothing to do."""
    if not t.focus or not user32.IsWindow(t.focus):
        return
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    thread = _thread_of(t.hwnd)
    if user32.GetGUIThreadInfo(thread, ctypes.byref(info)) and info.hwndFocus == t.focus:
        return
    me = kernel32.GetCurrentThreadId()
    if user32.AttachThreadInput(me, thread, True):
        try:
            user32.SetFocus(t.focus)
        finally:
            user32.AttachThreadInput(me, thread, False)
