"""Reach into the desktop for voice commands: keys, windows, apps, buttons.

Everything here is plain Win32 / UI Automation, no LLM. Keys LocalFlow sends
are stamped OWN_INPUT so its own keyboard hook lets them through.

  * Windows: the visible top-level windows, each with its program's file name
    ("chrome") and description ("Google Chrome"), so "switch to Chrome",
    "go to Visual Studio Code" and "show the terminal" all find something.
  * Apps: the Start menu's full app list (shell:AppsFolder), which covers
    both ordinary programs and Store apps, for "open Notepad".
  * Buttons: UI Automation lists the named controls of the window in front,
    the same tree screen readers use, for "click Send".

Window titles are matched but never logged: they can be personal.
"""

from __future__ import annotations

import ctypes
import difflib
import logging
import math
import os
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from .hotkey import OWN_INPUT

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_size_t]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p,
                                         wintypes.DWORD]

GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WM_CLOSE = 0x0010
SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_WHEEL = 0x2, 0x4, 0x800


def per_monitor_dpi() -> None:
    """Make this thread see real pixels on every monitor.

    With monitors at different scalings, a thread that isn't per-monitor
    aware gets window and control positions scaled to the main monitor's
    DPI. Numbered labels drawn in real pixels then missed their controls on
    every other screen. Every thread that reads or sets positions calls this.
    """
    try:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        pass


user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


# -- keys ------------------------------------------------------------------

KEYS = {
    "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "space": 0x20, "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "win": 0x5B,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "f4": 0x73, "f5": 0x74,
}
#: Keys that need the extended flag, or Windows takes them for the numpad.
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E, 0x5B}


def press(chord: str, times: int = 1) -> None:
    """Press a chord like "ctrl+shift+tab" (letters by name: "ctrl+a")."""
    vks = [KEYS[k] if k in KEYS else ord(k.upper()) for k in chord.split("+")]
    for _ in range(times):
        for vk in vks:
            user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0, OWN_INPUT)
        for vk in reversed(vks):
            flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
            user32.keybd_event(vk, 0, flags, OWN_INPUT)


def scroll(notches: int) -> None:
    """Mouse wheel over whatever is under the pointer. Positive = up."""
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, ctypes.c_uint32(notches * 120).value, OWN_INPUT)


MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x8, 0x10


def click_at(x: int, y: int, button: str = "left", double: bool = False) -> None:
    user32.SetCursorPos(x, y)
    down, up = ((MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if button == "right"
                else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    for _ in range(2 if double else 1):
        user32.mouse_event(down, 0, 0, 0, OWN_INPUT)
        user32.mouse_event(up, 0, 0, 0, OWN_INPUT)


# -- windows ---------------------------------------------------------------

@dataclass
class Win:
    hwnd: int
    title: str
    exe: str  # file stem, lower case: "chrome", "windowsterminal"
    description: str  # "Google Chrome", or "" if the file doesn't say

    def names(self) -> list[str]:
        """What a person might call it."""
        out = [self.exe, self.description]
        out += ALIASES_BY_EXE.get(self.exe, [])
        return [n.lower() for n in out if n]

    @property
    def label(self) -> str:
        """For the pill and the log: the program, never the window title."""
        return self.description or self.exe


#: Everyday names that don't appear in the program's own name.
ALIASES = {
    "browser": ["chrome", "msedge", "firefox", "brave", "opera", "vivaldi"],
    "web browser": ["chrome", "msedge", "firefox", "brave"],
    "edge": ["msedge"],
    "terminal": ["windowsterminal", "wt", "cmd", "powershell", "pwsh"],
    "command prompt": ["cmd", "windowsterminal"],
    "file explorer": ["explorer"],
    "explorer": ["explorer"],
    "files": ["explorer"],
    "vs code": ["code"],
    "vscode": ["code"],
    "code": ["code"],
    "word": ["winword"],
    "excel": ["excel"],
    "powerpoint": ["powerpnt"],
    "outlook": ["outlook", "olk"],
    "teams": ["ms-teams", "teams"],
    "settings": ["systemsettings"],
    "spotify": ["spotify"],
    "discord": ["discord"],
    "slack": ["slack"],
}
ALIASES_BY_EXE: dict[str, list[str]] = {}
for _name, _exes in ALIASES.items():
    for _exe in _exes:
        ALIASES_BY_EXE.setdefault(_exe, []).append(_name)

_desc_cache: dict[str, str] = {}


def _exe_path(hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def _description(path: str) -> str:
    if path not in _desc_cache:
        desc = ""
        try:
            import win32api

            lang, page = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")[0]
            desc = win32api.GetFileVersionInfo(
                path, f"\\StringFileInfo\\{lang:04x}{page:04x}\\FileDescription") or ""
        except Exception:
            pass
        _desc_cache[path] = desc.strip()
    return _desc_cache[path]


def _cloaked(hwnd: int) -> bool:
    """Hidden by the shell (another virtual desktop, a suspended Store app)."""
    value = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return bool(value.value)


def windows() -> list[Win]:
    """Visible app windows, front to back (the order Alt+Tab shows them)."""
    found: list[Win] = []

    def each(hwnd, _):
        if (not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER)
                or user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW
                or _cloaked(hwnd)):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if not buf.value:
            return True
        path = _exe_path(hwnd)
        exe = Path(path).stem.lower() if path else ""
        if exe in ("textinputhost", "shellexperiencehost", "searchhost", "startmenuexperiencehost"):
            return True
        if exe == "explorer" and buf.value == "Program Manager":
            return True  # the desktop itself
        found.append(Win(int(hwnd), buf.value, exe, _description(path) if path else ""))
        return True

    user32.EnumWindows(WNDENUMPROC(each), 0)
    return found


def name_score(spoken: str, names: list[str]) -> float:
    """How well a spoken name fits a list of names for one thing."""
    spoken = " ".join(_words(spoken))
    if not spoken:
        return 0.0
    best = 0.0
    for name in names:
        name = " ".join(_words(name))
        if not name:
            continue
        best = max(best, sim(spoken, name), sim(spoken.replace(" ", ""), name.replace(" ", "")))
        # "chrome" in "google chrome": each spoken word found in the name.
        words = name.split()
        hits = [max(sim(w, x) for x in words) for w in spoken.split()]
        if hits and min(hits) >= 0.85:
            best = max(best, 0.9 * min(hits))
    return best


_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
         "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
         "eighteen": 18, "nineteen": 19,
         # what Whisper writes for a lone number now and then
         "won": 1, "to": 2, "too": 2, "for": 4, "ate": 8}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}


def number(text: str) -> int | None:
    """ "17", "seventeen", "number twenty one", "one hundred and five" -> int."""
    words = [w for w in text.lower().replace("-", " ").split() if w not in ("number", "and", "#")]
    if len(words) == 1 and words[0].isdigit():
        return int(words[0])
    total, seen = 0, False
    for w in words:
        if w.isdigit():
            total += int(w)
        elif w in _ONES:
            total += _ONES[w]
        elif w in _TENS:
            total += _TENS[w]
        elif w == "hundred" and seen:
            total *= 100
        else:
            return None
        seen = True
    return total if seen else None


# -- the window list ("show windows") ------------------------------------------------

#: The windows numbered in the list on screen now (or last shown): "switch to
#: 3" means numbered[2]. Several terminals look alike by name; by number
#: they don't.
numbered: list[Win] = []
#: Set by the overlay: draws [(number, "line"), ...] as a list, or [] hides it.
list_sink = None


def show_window_list(wins: list[Win]) -> bool:
    numbered[:] = wins
    if list_sink is None:
        return False
    # The chips are drawn above the list, so every window's number shows.
    show_labels(window_chips(wins), "chip")
    mons = monitors()
    lines = []
    for i, w in enumerate(wins):
        mon = monitor_of(w.hwnd, mons)
        where = f"   ·  screen {mon}" if len(mons) > 1 and mon else ""
        lines.append((i + 1, list_line(w) + where))
    list_sink(lines)
    return True


#: Short program names for the list, where the description is long or vague.
SHORT = {"windowsterminal": "Terminal", "cmd": "Command Prompt", "powershell": "PowerShell",
         "pwsh": "PowerShell", "msedge": "Edge", "chrome": "Chrome", "explorer": "File Explorer",
         "code": "VS Code", "winword": "Word", "excel": "Excel", "outlook": "Outlook"}


def list_line(w: Win) -> str:
    """"Localflow setup and usage  (Terminal)": the title first, since that's
    what tells two terminals apart; the program after it unless the title
    already says it."""
    prog = SHORT.get(w.exe) or w.description or w.exe
    title = w.title.strip()
    for suffix in (f" - {w.description}", f" - {prog}", " - File Explorer", " - Google Chrome",
                   " - Microsoft Edge", " - Notepad++"):
        if suffix.strip(" -") and title.endswith(suffix):
            title = title[: -len(suffix)]
    if not title or title.lower() == prog.lower():
        return prog
    title = title if len(title) <= 70 else title[:69] + "…"
    return f"{title}  ({prog})"




CHIP_W, CHIP_H = 56, 40


def window_chips(wins: list[Win]) -> list[tuple[int, tuple[int, int, int, int]]]:
    """(number, chip box) for every listed window that isn't minimised, at its
    top-left corner, drawn above everything (the list too). A window behind
    another one gets its chip all the same: telling them apart is the point.
    It used to be skipped. Chips that would land on each other (windows
    stacked at the same spot) move along to the right; all stay on screen."""
    mons = monitors()
    placed: list[tuple[int, int, int, int]] = []
    chips = []
    for i, w in enumerate(wins):
        if user32.IsIconic(w.hwnd):
            continue
        left, top, right, bottom = window_rect(w.hwnd)
        if right - left < 80 or bottom - top < 40:
            continue
        n = monitor_of(w.hwnd, mons)
        area = mons[n - 1][2] if 1 <= n <= len(mons) else None
        x, y = left + 8, top + 6
        if area is not None:  # a maximised window's corner is just off screen
            x = max(area.left + 4, min(x, area.right - CHIP_W - 4))
            y = max(area.top + 4, min(y, area.bottom - CHIP_H - 4))
        box = (x, y, x + CHIP_W, y + CHIP_H)
        while any(box[0] < p[2] and p[0] < box[2] and box[1] < p[3] and p[1] < box[3]
                  for p in placed):
            box = (box[0] + CHIP_W + 6, box[1], box[2] + CHIP_W + 6, box[3])
        placed.append(box)
        chips.append((i + 1, box))
    return chips


def hide_window_list() -> None:
    if list_sink is not None:
        list_sink([])


#: Set by the overlay: draws the command sheet [(section, [(say, does)])], [] hides it.
help_sink = None


def show_help(sections: list) -> bool:
    if help_sink is None:
        return False
    help_sink(sections)
    return True


def hide_help() -> None:
    if help_sink is not None:
        help_sink([])


# -- monitors, numbered left to right ---------------------------------------------------

MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


def monitors() -> list[tuple[int, wintypes.RECT, wintypes.RECT]]:
    """(handle, whole screen, work area) per monitor, numbered left to right
    (then top to bottom) - the order you see them on the desk."""
    found = []

    def each(h, _hdc, _r, _l):
        info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
        user32.GetMonitorInfoW(h, ctypes.byref(info))
        found.append((int(h), info.rcMonitor, info.rcWork))
        return True

    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(each), 0)
    found.sort(key=lambda m: (m[1].left, m[1].top))
    return found


def monitor_of(hwnd: int, mons=None) -> int:
    """1-based number of the monitor a window is (mostly) on."""
    mons = mons or monitors()
    h = int(user32.MonitorFromWindow(hwnd, 2) or 0)
    for i, (mh, _, _) in enumerate(mons):
        if mh == h:
            return i + 1
    return 0


def to_monitor(hwnd: int, n: int) -> None:
    """Move a window to monitor n (1-based), keeping its size; a maximised
    window comes up maximised there."""
    mons = monitors()
    if not 1 <= n <= len(mons):
        raise ValueError(f"there is no screen {n} (there are {len(mons)})")
    work = mons[n - 1][2]
    zoomed = bool(user32.IsZoomed(hwnd))
    if zoomed or user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)
    l, t, r, b = window_rect(hwnd)
    w = min(r - l, work.right - work.left)
    h = min(b - t, work.bottom - work.top)
    x = work.left + (work.right - work.left - w) // 2
    y = work.top + (work.bottom - work.top - h) // 2
    user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0004 | 0x0010)
    if zoomed:
        time.sleep(0.1)
        user32.ShowWindow(hwnd, SW_MAXIMIZE)


def find_window(spoken: str, wins: list[Win] | None = None) -> tuple[Win | None, float]:
    """The window best fitting a spoken name: its program first, then its title.
    A number means that window in the list from "show windows"."""
    n = number(spoken)
    if n is not None and numbered:
        if 1 <= n <= len(numbered) and user32.IsWindow(numbered[n - 1].hwnd):
            return numbered[n - 1], 1.0
        return None, 0.0
    wins = windows() if wins is None else wins
    best, best_score = None, 0.0
    for w in wins:
        score = name_score(spoken, w.names())
        # The title counts a little less: "switch to budget" -> "Budget.xlsx - Excel".
        title_words = _words(w.title)
        spoken_words = _words(spoken)
        if title_words and spoken_words:
            hits = [max(sim(s, t) for t in title_words) for s in spoken_words]
            score = max(score, 0.88 * min(hits))
        if score > best_score:  # ties: the one nearer the front wins
            best, best_score = w, score
    return best, best_score


def foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def focus(w: Win) -> bool:
    from . import window

    return window.activate(window.Target(w.hwnd, None, ""))


def show(hwnd: int, how: int) -> None:
    user32.ShowWindow(hwnd, how)


def close(hwnd: int) -> None:
    """Ask the window to close, as its X button would (it may ask to save)."""
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


# -- apps ------------------------------------------------------------------

@dataclass
class App:
    name: str
    parsing_name: str  # what shell:AppsFolder\<this> launches


_apps: list[App] = []
_apps_at = 0.0
_apps_lock = threading.Lock()


def apps(max_age: float = 300.0) -> list[App]:
    """The Start menu's app list (desktop programs and Store apps)."""
    global _apps, _apps_at
    with _apps_lock:
        if _apps and time.monotonic() - _apps_at < max_age:
            return _apps
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        out = []
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            folder = shell.NameSpace("shell:AppsFolder")
            for item in folder.Items():
                name, path = item.Name, item.Path
                if name and path and not re.search(r"uninstall|readme|help|documentation",
                                                   name, re.I):
                    out.append(App(name, path))
        except Exception as exc:
            log.warning("could not read the app list: %s", exc)
        _apps, _apps_at = out, time.monotonic()
        return out


def find_app(spoken: str) -> tuple[App | None, float]:
    best, best_score = None, 0.0
    for app in apps():
        names = [app.name] + [a for a, exes in ALIASES.items()
                              if any(e in app.parsing_name.lower() for e in exes)]
        score = name_score(spoken, names)
        if score > best_score or (score == best_score and best and len(app.name) < len(best.name)):
            best, best_score = app, score
    return best, best_score


def launch(app: App) -> None:
    os.startfile("shell:AppsFolder\\" + app.parsing_name)


def open_url(url: str) -> None:
    os.startfile(url)


# -- buttons (UI Automation) -------------------------------------------------

_uia_local = threading.local()

#: Control types worth clicking: buttons, links, menu items, tabs, list and
#: tree items, check boxes, radio buttons, combo boxes, split buttons.
_CLICKABLE = {50000, 50005, 50011, 50019, 50007, 50024, 50002, 50013, 50003, 50031,
              50004}  # (not Text: a link's text is reported apart from the link)
#: Clickable only when they say so (an Invoke or selection pattern, or
#: keyboard focus): text, images, custom, groups, data items, documents,
#: header items, sliders, spinners. Web pages and Electron apps make their
#: own buttons from these; a census of a browser tab found 35 clickable
#: images and 29 clickable groups that got no number. Panes, toolbars and
#: menu bars stay out: they're containers, and often focusable.
_CLICKABLE_IF_ACTIVE = {50020, 50006, 50025, 50026, 50029, 50030, 50035, 50015, 50016}
UIA_NAME, UIA_TYPE, UIA_RECT = 30005, 30003, 30001
UIA_ENABLED, UIA_OFFSCREEN = 30010, 30022
UIA_FOCUSABLE, UIA_HAS_INVOKE, UIA_HAS_SELECT = 30009, 30031, 30036
#: Windows already asked once: Chromium browsers and Electron apps build
#: their page's accessibility tree only when something asks, so the first
#: look at a window found the toolbar (22) but not the page (71).
_woken: set[int] = set()
UIA_INVOKE, UIA_SELECTION_ITEM, UIA_TOGGLE, UIA_EXPAND = 10000, 10010, 10015, 10005


def _automation():
    """This thread's UI Automation client (COM objects stay on their thread)."""
    uia = getattr(_uia_local, "uia", None)
    if uia is None:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        uia = (comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation), UIA)
        _uia_local.uia = uia
    return uia


@dataclass
class Control:
    name: str
    kind: int
    rect: tuple[int, int, int, int]
    element: object


def controls(hwnd: int, named: bool = True) -> list[Control]:
    """Enabled, on-screen clickable controls of a window; only named ones
    unless ``named`` is False (numbered labels need no name)."""
    uia, UIA = _automation()
    import comtypes

    comtypes.CoInitialize()  # per thread; harmless if already done
    root = uia.ElementFromHandle(hwnd)
    cache = uia.CreateCacheRequest()
    for prop in (UIA_NAME, UIA_TYPE, UIA_RECT, UIA_ENABLED, UIA_OFFSCREEN, UIA_FOCUSABLE,
                 UIA_HAS_INVOKE, UIA_HAS_SELECT):
        cache.AddProperty(prop)
    cond = uia.CreateAndCondition(
        uia.CreatePropertyCondition(UIA_ENABLED, True),
        uia.CreatePropertyCondition(UIA_OFFSCREEN, False))
    per_monitor_dpi()
    started = time.perf_counter()
    if hwnd not in _woken:
        _woken.add(hwnd)
        root.FindAllBuildCache(4, cond, cache)  # wakes a lazy tree; then look again
        time.sleep(0.5)
    found = root.FindAllBuildCache(4, cond, cache)  # TreeScope_Descendants
    out = []
    for i in range(found.Length):
        el = found.GetElement(i)
        try:
            kind, name = el.CachedControlType, (el.CachedName or "").strip()
        except Exception:
            continue
        if kind not in _CLICKABLE:
            if kind not in _CLICKABLE_IF_ACTIVE:
                continue
            try:
                active = any(el.GetCachedPropertyValue(p) for p in
                             (UIA_HAS_INVOKE, UIA_HAS_SELECT, UIA_FOCUSABLE))
            except Exception:
                active = False
            if not active:
                continue
        if named and not name:
            continue
        r = el.CachedBoundingRectangle
        if r.right - r.left < 4 or r.bottom - r.top < 4:
            continue  # collapsed or hidden
        out.append(Control(name, kind, (r.left, r.top, r.right, r.bottom), el))
    log.debug("%d clickable controls in %.2fs", len(out), time.perf_counter() - started)
    return out


#: Where "move it to the top right" puts a window: fractions of the work area
#: of the monitor the window is on (left, top, right, bottom).
PLACES = {
    "left": (0, 0, .5, 1), "right": (.5, 0, 1, 1), "top": (0, 0, 1, .5), "bottom": (0, .5, 1, 1),
    "top left": (0, 0, .5, .5), "top right": (.5, 0, 1, .5),
    "bottom left": (0, .5, .5, 1), "bottom right": (.5, .5, 1, 1),
    "full": (0, 0, 1, 1),
}


def place(hwnd: int, where: str, monitor: int = 0) -> None:
    """Fill a half or a quarter of the window's own monitor (or monitor n)."""
    fl, ft, fr, fb = PLACES[where]
    if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)
    if monitor:
        mons = monitors()
        if not 1 <= monitor <= len(mons):
            raise ValueError(f"there is no screen {monitor} (there are {len(mons)})")
        a = mons[monitor - 1][2]
    else:
        a = work_area(hwnd)
    w, h = a.right - a.left, a.bottom - a.top
    user32.SetWindowPos(hwnd, 0, a.left + int(w * fl), a.top + int(h * ft),
                        int(w * (fr - fl)), int(h * (fb - ft)), 0x0004 | 0x0010)


def center(hwnd: int) -> None:
    """Centre a window on its monitor's work area (restored first if maximised)."""
    if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    area = work_area(hwnd)
    w, h = rect.right - rect.left, rect.bottom - rect.top
    w, h = min(w, area.right - area.left), min(h, area.bottom - area.top)
    x = area.left + (area.right - area.left - w) // 2
    y = area.top + (area.bottom - area.top - h) // 2
    user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
                                       wintypes.LPARAM]
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


def get_placement(hwnd: int) -> tuple[WINDOWPLACEMENT, tuple[int, int, int, int]] | None:
    """Everything needed to put a window back: its state (normal, maximised,
    minimised), its normal position, and where it actually is now."""
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    wp = WINDOWPLACEMENT(length=ctypes.sizeof(WINDOWPLACEMENT))
    if not user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return None
    return wp, window_rect(hwnd)


def set_placement(hwnd: int, saved) -> bool:
    if not saved or not user32.IsWindow(hwnd):
        return False
    wp, (l, t, r, b) = saved
    user32.SetWindowPlacement(hwnd, ctypes.byref(wp))
    if wp.showCmd == 1:  # SW_SHOWNORMAL: the exact spot, not just the normal rect
        user32.SetWindowPos(hwnd, 0, l, t, r - l, b - t, 0x0004 | 0x0010)
    return True


# -- arranging windows and saved layouts ---------------------------------------

DWMWA_EXTENDED_FRAME_BOUNDS = 9


def _set_visible(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    """Put a window's visible frame at x, y, w, h. Windows 10 and 11 frames
    have invisible resize borders, so tiling by the window rect leaves gaps."""
    l, t, r, b = window_rect(hwnd)
    vis = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(vis),
                                    ctypes.sizeof(vis)) == 0:
        dl, dt, dr, db = vis.left - l, vis.top - t, r - vis.right, b - vis.bottom
    else:
        dl = dt = dr = db = 0
    user32.SetWindowPos(hwnd, 0, x - dl, y - dt, w + dl + dr, h + dt + db, 0x0004 | 0x0010)


#: Arrangement styles: "equal" (the default: tiles as equal as can be, each
#: as close to a normal window's shape as can be), "columns" (side by
#: side), "rows" (stacked), "grid" (as square as can be), "main" (the front
#: window big, the rest beside it), "cascade".
STYLES = ("equal", "main", "columns", "rows", "grid", "cascade")
#: A window's natural shape, width / height: what "equal" aims each tile at.
NICE_ASPECT = 1.5


def _rows(n: int, rows: int, area) -> list[tuple[int, int, int, int]]:
    """n tiles in ``rows`` rows filling the area; a short row's tiles are wider."""
    left, top, right, bottom = area
    w, h = right - left, bottom - top
    out = []
    for row in range(rows):
        cols = n // rows + (1 if row < n % rows else 0)
        y0, y1 = top + h * row // rows, top + h * (row + 1) // rows
        for col in range(cols):
            out.append((left + w * col // cols, y0, left + w * (col + 1) // cols, y1))
    return out


def grid(n: int, area: tuple[int, int, int, int], style: str = "equal"
         ) -> list[tuple[int, int, int, int]]:
    """n tiles filling an area (left, top, right, bottom), row by row (the
    order windows go in: top row first, left to right). See STYLES."""
    left, top, right, bottom = area
    w, h = max(right - left, 1), max(bottom - top, 1)
    if n <= 0:
        return []
    if style == "columns":
        return _rows(n, 1, area)
    if style == "rows":
        return [(left, top + h * k // n, right, top + h * (k + 1) // n) for k in range(n)]
    if style == "grid":
        return _rows(n, max(1, min(n, round(math.sqrt(n * h / w * 1.2)))), area)
    if style == "main" and n > 1:
        # The first window big (60 %), the rest stacked beside it, or under
        # it on a portrait screen.
        if w >= h:
            split = left + w * 3 // 5
            return [(left, top, split, bottom)] + grid(n - 1, (split, top, right, bottom), "rows")
        split = top + h * 3 // 5
        return [(left, top, right, split)] + grid(n - 1, (left, split, right, bottom), "columns")
    if style == "cascade":
        step = max(24, min(w, h) // 20)
        cw, ch = w * 7 // 10, h * 7 // 10
        out = []
        for k in range(n):
            o = (k * step) % max(1, min(w - cw, h - ch) + 1)
            out.append((left + o, top + o, left + o + cw, top + o + ch))
        return out
    # "equal": every rows x columns choice; the best has tiles all the same
    # size, shaped most like a window. Three on a portrait screen stack; three
    # on a wide one sit side by side (the old square-ish rule put two beside
    # each other and one wide one under them on a portrait screen).
    best, best_score = 1, float("inf")
    for rows in range(1, n + 1):
        cols = -(-n // rows)
        aspect = (w / cols) / (h / rows)
        score = abs(math.log(aspect / NICE_ASPECT)) + (1.0 if rows * cols != n else 0.0)
        if score < best_score - 1e-9:
            best, best_score = rows, score
    return _rows(n, best, area)


def arrange(wins: list[Win] | None = None, style: str = "equal", monitor: int = 0) -> int:
    """Tile the windows open on each screen (not minimised ones) so every one
    can be seen, in a style (STYLES). Each stays on its own screen; ones on
    the left stay left. ``monitor``: only that screen (1-based)."""
    wins = [w for w in (windows() if wins is None else wins) if not user32.IsIconic(w.hwnd)]
    mons = monitors()
    if monitor and not 1 <= monitor <= len(mons):
        raise ValueError(f"there is no screen {monitor} (there are {len(mons)})")
    by_screen: dict[int, list[Win]] = {}
    for w in wins:  # front to back, so a group's first window is its front one
        by_screen.setdefault(monitor_of(w.hwnd, mons), []).append(w)
    done = 0
    for n, group in by_screen.items():
        if not 1 <= n <= len(mons) or (monitor and n != monitor):
            continue
        work = mons[n - 1][2]
        tiles = grid(len(group), (work.left, work.top, work.right, work.bottom), style)

        def centre(w: Win) -> tuple[float, float]:
            l, t, r, b = window_rect(w.hwnd)
            return (l + r) / 2, (t + b) / 2

        if style in ("main", "cascade"):
            # Main: the front window gets the big tile. Cascade: the front
            # window ends up on top, so it goes last.
            ordered = group if style == "main" else group[::-1]
        else:
            # Top ones to the top row, then left to right within each row.
            group.sort(key=lambda w: centre(w)[1])
            ordered, i = [], 0
            for row_top in sorted({t[1] for t in tiles}):
                k = sum(1 for t in tiles if t[1] == row_top)
                ordered += sorted(group[i:i + k], key=lambda w: centre(w)[0])
                i += k
        for w, (l, t, r, b) in zip(ordered, tiles):
            if user32.IsZoomed(w.hwnd):
                user32.ShowWindow(w.hwnd, SW_RESTORE)
            _set_visible(w.hwnd, l, t, r - l, b - t)
            if style == "cascade":
                user32.SetWindowPos(w.hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # to the top
            done += 1
    return done


def _placement_dict(hwnd: int) -> dict | None:
    got = get_placement(hwnd)
    if got is None:
        return None
    wp, rect = got
    n = wp.rcNormalPosition
    return {"show": wp.showCmd, "flags": wp.flags,
            "min": [wp.ptMinPosition.x, wp.ptMinPosition.y],
            "max": [wp.ptMaxPosition.x, wp.ptMaxPosition.y],
            "normal": [n.left, n.top, n.right, n.bottom], "rect": list(rect)}


def _apply_placement(hwnd: int, d: dict) -> bool:
    wp = WINDOWPLACEMENT(length=ctypes.sizeof(WINDOWPLACEMENT))
    wp.flags, wp.showCmd = d["flags"], d["show"]
    wp.ptMinPosition = wintypes.POINT(*d["min"])
    wp.ptMaxPosition = wintypes.POINT(*d["max"])
    wp.rcNormalPosition = wintypes.RECT(*d["normal"])
    return set_placement(hwnd, (wp, tuple(d["rect"])))


def snapshot() -> list[dict]:
    """Every app window: which program, its title (to tell two of the same
    program apart) and exactly where it is. Front to back."""
    out = []
    for w in windows():
        place_ = _placement_dict(w.hwnd)
        if place_:
            out.append({"hwnd": w.hwnd, "exe": w.exe, "path": _exe_path(w.hwnd),
                        "title": w.title, **place_})
    return out


#: Programs that can't be started again from their file to fill a layout.
_NO_RELAUNCH = {"applicationframehost", "explorer", ""}


def restore(entries: list[dict], launch_missing: bool = True,
            wait: float = 10.0) -> tuple[int, int, list[str]]:
    """Put windows back as saved: (placed, opened, programs not found).

    Each saved window goes to the open window of the same program with the
    closest title (the same window, if it's still open). A program with no
    window open is started, when ``launch_missing``, and placed once its
    window appears."""
    used: set[int] = set()
    cur = windows()
    placed, missing = 0, []

    def match(e: dict, wins: list[Win]) -> Win | None:
        same = [w for w in wins if w.hwnd == e.get("hwnd") and w.exe == e["exe"]]
        cands = same or [w for w in wins if w.exe == e["exe"] and w.hwnd not in used]
        if not cands:
            return None
        return max(cands, key=lambda w: difflib.SequenceMatcher(None, w.title, e["title"]).ratio())

    for e in entries:
        w = match(e, cur)
        if w is None:
            missing.append(e)
            continue
        used.add(w.hwnd)
        placed += _apply_placement(w.hwnd, e)
    opened, not_found = 0, []
    if launch_missing:
        started = []
        for e in missing[:8]:
            path = e.get("path", "")
            if e["exe"] in _NO_RELAUNCH or not path or not Path(path).exists():
                not_found.append(e["exe"])
                continue
            try:
                os.startfile(path)
                started.append(e)
            except OSError:
                not_found.append(e["exe"])
        deadline = time.monotonic() + wait
        while started and time.monotonic() < deadline:
            time.sleep(0.5)
            wins = [w for w in windows() if w.hwnd not in used]
            for e in list(started):
                w = match(e, wins)
                if w is not None:
                    time.sleep(0.4)  # let it finish opening, or it moves itself back
                    used.add(w.hwnd)
                    opened += _apply_placement(w.hwnd, e)
                    started.remove(e)
        not_found += [e["exe"] for e in started]
    else:
        not_found = [e["exe"] for e in missing]
    return placed, opened, not_found


def sleep_pc() -> None:
    """Put the PC to sleep (not hibernate), as Start > Power > Sleep does."""
    import win32api
    import win32security

    # Needs the shutdown privilege switched on in our own token first.
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32security.TOKEN_ADJUST_PRIVILEGES | win32security.TOKEN_QUERY)
    luid = win32security.LookupPrivilegeValue(None, win32security.SE_SHUTDOWN_NAME)
    win32security.AdjustTokenPrivileges(token, False, [(luid, win32security.SE_PRIVILEGE_ENABLED)])
    # SetSuspendState(hibernate=False, force=False, wakeup_events_disabled=False)
    if not ctypes.WinDLL("powrprof").SetSuspendState(False, False, False):
        raise OSError(f"SetSuspendState failed (error {ctypes.get_last_error()})")


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def work_area(hwnd: int) -> wintypes.RECT:
    info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
    return info.rcWork


# -- where the text went (the paste effect) -------------------------------------

class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


UIA_TEXT2 = 10024  # TextPattern2: has the caret's range


def caret_point() -> tuple[int, int] | None:
    """Where the text cursor is now, on screen: Windows' caret (classic
    apps), else UI Automation's caret range (browsers, Electron, modern
    apps), else the middle of the focused control, else the window's."""
    per_monitor_dpi()
    fg = user32.GetForegroundWindow()
    if not fg:
        return None
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    tid = user32.GetWindowThreadProcessId(fg, None)
    if user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndCaret:
        rc = info.rcCaret
        if rc.bottom > rc.top:
            pt = wintypes.POINT(rc.left, (rc.top + rc.bottom) // 2)
            user32.ClientToScreen(info.hwndCaret, ctypes.byref(pt))
            return pt.x, pt.y
    try:
        uia, UIA = _automation()
        el = uia.GetFocusedElement()
        try:
            pattern = el.GetCurrentPatternAs(UIA_TEXT2, UIA.IUIAutomationTextPattern2._iid_)
            if pattern:
                rng = pattern.QueryInterface(UIA.IUIAutomationTextPattern2).GetCaretRange()[1]
                rects = list(rng.GetBoundingRectangles() or [])
                if len(rects) >= 4 and rects[3] > 0:
                    return int(rects[0] + rects[2]), int(rects[1] + rects[3] / 2)
        except Exception:
            pass
        r = el.CurrentBoundingRectangle
        if r.right > r.left and r.bottom > r.top:
            return (r.left + r.right) // 2, (r.top + r.bottom) // 2
    except Exception:
        log.debug("no caret from UI Automation", exc_info=True)
    left, top, right, bottom = window_rect(fg)
    return (left + right) // 2, (top + bottom) // 2


#: Set by the overlay: effect_sink((x, y)) plays the theme's paste effect there.
effect_sink = None


def paste_effect() -> None:
    """The theme's little burst where the text just went, if the overlay is up."""
    if effect_sink is None:
        return
    try:
        point = caret_point()
    except Exception:
        log.debug("caret lookup failed", exc_info=True)
        return
    if point is not None:
        effect_sink(point)


# -- terminal paste warnings ---------------------------------------------------

#: The button on Windows Terminal's "paste more than 5 KiB" and "paste
#: multiple lines" warnings.
PASTE_ANYWAY = "Paste anyway"


def confirm_terminal_paste(timeout: float = 1.0) -> bool:
    """If Windows Terminal stopped a paste with a warning, press its "Paste
    anyway". Asking LocalFlow to paste is the confirmation. Only that named
    button is ever pressed, and only if it's there: never a blind Enter,
    which would send the message instead. True if it pressed it."""
    hwnd = foreground()
    if not hwnd or Path(_exe_path(hwnd)).stem.lower() != "windowsterminal":
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.15)
        try:
            for c in controls(hwnd):
                if c.kind == 50000 and c.name == PASTE_ANYWAY:
                    activate_control(c)
                    log.info("pressed Windows Terminal's 'Paste anyway'")
                    time.sleep(0.2)
                    return True
        except Exception:
            log.debug("could not look for the paste warning", exc_info=True)
            return False
    return False


# -- numbered labels ---------------------------------------------------------

#: Set by the overlay: draws [(number, (left, top, right, bottom)), ...] over
#: the screen, or clears them with []. None when there is no overlay.
label_sink = None


def show_labels(labels: list[tuple[int, tuple[int, int, int, int]]], style: str = "label") -> bool:
    """Numbers over the screen. style: "label" (a control, outlined), "big"
    (a screen's number), "chip" (a window's number in its corner), "grid"
    (numbered cells)."""
    if label_sink is None:
        return False
    label_sink(labels, style)
    return True


def find_control(spoken: str, items: list[Control]) -> tuple[Control | None, float]:
    best, best_score = None, 0.0
    for c in items:
        score = name_score(spoken, [c.name])
        if score > best_score:
            best, best_score = c, score
    return best, best_score


def activate_control(c: Control) -> None:
    """Invoke it the way UI Automation offers; click its middle as a last resort."""
    _, UIA = _automation()
    for pattern, iface, method in (
        (UIA_INVOKE, UIA.IUIAutomationInvokePattern, "Invoke"),
        (UIA_SELECTION_ITEM, UIA.IUIAutomationSelectionItemPattern, "Select"),
        (UIA_TOGGLE, UIA.IUIAutomationTogglePattern, "Toggle"),
        (UIA_EXPAND, UIA.IUIAutomationExpandCollapsePattern, "Expand"),
    ):
        try:
            p = c.element.GetCurrentPatternAs(pattern, iface._iid_)
            if p:
                getattr(p.QueryInterface(iface), method)()
                return
        except Exception:
            continue
    left, top, right, bottom = c.rect
    click_at((left + right) // 2, (top + bottom) // 2)
