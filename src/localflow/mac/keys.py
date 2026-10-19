"""Keys on a Mac, without pynput: a Quartz event tap to hear the dictation
key, and posted key events to press shortcuts and paste.

pynput's Mac keyboard code looks keys up in the keyboard layout (TIS /
TSM calls), which recent macOS only allows on the main thread: its
listener and its Controller, run from a worker thread, abort the whole
process without a word (seen in CI: the app died as the hotkeys started).
Everything here works on key codes only, so any thread is fine.

Needs Input Monitoring (to hear keys) and Accessibility (to post them),
granted to the Python that runs Golden Flow.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

#: The Mac's key codes (US layout positions; shortcuts use these too).
CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "enter": 36, "l": 37,
    "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "tab": 48, "space": 49, "`": 50, "backspace": 51, "escape": 53,
    "cmd": 55, "shift": 56, "caps_lock": 57, "alt": 58, "ctrl": 59, "shift_r": 60,
    "alt_r": 61, "ctrl_r": 62, "cmd_r": 54, "fn": 63,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111, "home": 115, "pageup": 116,
    "delete": 117, "end": 119, "pagedown": 121, "left": 123, "right": 124, "down": 125,
    "up": 126,
}
#: Key code -> the Windows virtual-key code hotkey.py works in.
TO_VK = {
    55: 91, 54: 91, 56: 16, 60: 16, 59: 17, 62: 17, 58: 18, 61: 0xA5, 57: 20, 53: 27,
    49: 32, 36: 13, 48: 9, 51: 8, 117: 46, 123: 37, 124: 39, 125: 40, 126: 38,
    115: 36, 119: 35, 116: 33, 121: 34,
    **{code: 0x6F + n for n, code in enumerate((122, 120, 99, 118, 96, 97, 98, 100, 101, 109,
                                                103, 111), start=1)},
    **{code: ord(ch.upper()) for ch, code in CODES.items() if len(ch) == 1 and ch.isalnum()},
}
#: Modifier keys come as "flags changed"; their key codes.
MODIFIERS = {54, 55, 56, 57, 58, 59, 60, 61, 62, 63}
#: CGEventFlags for the modifiers a posted shortcut holds.
FLAGS = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "cmd": 1 << 20}


class Listener:
    """Hears every key (listen-only: nothing is blocked) and reports
    Windows-style virtual-key codes to on_down / on_up."""

    def __init__(self, on_down: Callable[[int], None], on_up: Callable[[int], None]):
        self.on_down, self.on_up = on_down, on_up
        self._loop = None
        self._held: set[int] = set()
        self._ready = threading.Event()
        self.ok = False

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="localflow-keys").start()
        self._ready.wait(3)

    def stop(self) -> None:
        if self._loop is not None:
            import Quartz

            Quartz.CFRunLoopStop(self._loop)
            self._loop = None

    def _run(self) -> None:
        import Quartz

        mask = ((1 << Quartz.kCGEventKeyDown) | (1 << Quartz.kCGEventKeyUp)
                | (1 << Quartz.kCGEventFlagsChanged))
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly, mask, self._event, None)
        if self._tap is None:
            log.warning("can't hear the keyboard: allow Input Monitoring for this Python in "
                        "System Settings > Privacy & Security, then restart Golden Flow")
            self._ready.set()
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        self.ok = True
        self._ready.set()
        Quartz.CFRunLoopRun()

    def _event(self, proxy, kind, event, refcon):
        import Quartz

        try:
            if kind in (Quartz.kCGEventTapDisabledByTimeout,
                        Quartz.kCGEventTapDisabledByUserInput):
                Quartz.CGEventTapEnable(self._tap, True)  # macOS turned it off: back on
                return event
            code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            vk = TO_VK.get(code)
            if vk is None:
                return event
            if kind == Quartz.kCGEventFlagsChanged:
                down = code not in self._held  # a modifier's own press or release
            else:
                down = kind == Quartz.kCGEventKeyDown
            if down:
                self._held.add(code)
                self.on_down(vk)
            else:
                self._held.discard(code)
                self.on_up(vk)
        except Exception:
            log.debug("key event failed", exc_info=True)
        return event


def _post(code: int, down: bool, flags: int = 0) -> None:
    import Quartz

    e = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    if flags:
        Quartz.CGEventSetFlags(e, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


def press(chord: str, times: int = 1) -> None:
    """Press a chord by key names ("cmd+shift+t"); any thread."""
    names = chord.lower().split("+")
    mods = [n for n in names if n in FLAGS]
    keys = [n for n in names if n not in FLAGS]
    flags = 0
    for m in mods:
        flags |= FLAGS[m]
    for _ in range(times):
        for m in mods:
            _post(CODES[m], True, flags)
        for k in keys:
            _post(CODES[k], True, flags)
        for k in reversed(keys):
            _post(CODES[k], False, flags)
        for m in reversed(mods):
            _post(CODES[m], False)
        time.sleep(0.01)


def type_text(text: str, interval: float = 0.0) -> None:
    """Type text as characters (no key codes, so any layout)."""
    import Quartz

    for ch in text:
        for down in (True, False):
            e = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(e, len(ch.encode("utf-16-le")) // 2, ch)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        if interval:
            time.sleep(interval)
