"""Put text into whatever window currently has focus (Windows)."""

from __future__ import annotations

import logging
import time

from .config import InjectConfig

log = logging.getLogger(__name__)


class InjectError(RuntimeError):
    pass


def _clipboard_retry(fn, attempts: int = 8, delay: float = 0.02):
    """Clipboard ops fail transiently while another process holds the lock."""
    import pywintypes

    last: Exception | None = None
    for _ in range(attempts):
        try:
            return fn()
        except pywintypes.error as exc:
            last = exc
            time.sleep(delay)
    raise InjectError(f"clipboard unavailable: {last}")


def get_clipboard_text() -> str | None:
    import win32clipboard as cb
    import win32con

    def _read():
        cb.OpenClipboard()
        try:
            if cb.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return cb.GetClipboardData(win32con.CF_UNICODETEXT)
            return None
        finally:
            cb.CloseClipboard()

    return _clipboard_retry(_read)


def set_clipboard_text(text: str) -> None:
    import win32clipboard as cb
    import win32con

    def _write():
        cb.OpenClipboard()
        try:
            cb.EmptyClipboard()
            cb.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            cb.CloseClipboard()

    _clipboard_retry(_write)


#: Programs whose windows are terminals (file names, lower case, no .exe).
TERMINALS = {"windowsterminal", "wt", "cmd", "powershell", "pwsh", "conhost", "openconsole",
             "alacritty", "wezterm-gui", "mintty", "tabby", "hyper", "warp"}


def _program() -> str:
    """The program in front, for the log (never its window title)."""
    try:
        from pathlib import Path

        from .desktop import _exe_path, foreground

        return Path(_exe_path(foreground())).stem.lower() or "?"
    except Exception:
        return "?"


def in_terminal() -> bool:
    """Is the window in front a terminal? A window a PowerShell script
    opened (WinForms, WPF) belongs to powershell.exe but isn't one."""
    try:
        import ctypes
        from pathlib import Path

        from .desktop import _exe_path, foreground

        hwnd = foreground()
        if Path(_exe_path(hwnd)).stem.lower() not in TERMINALS:
            return False
        buf = ctypes.create_unicode_buffer(128)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 128)
        return not buf.value.startswith(("WindowsForms", "HwndWrapper"))
    except Exception:
        return False


class Injector:
    def __init__(self, cfg: InjectConfig):
        self.cfg = cfg
        self._kb = None

    @property
    def keyboard(self):
        if self._kb is None:
            from pynput.keyboard import Controller

            self._kb = Controller()
        return self._kb

    def send(self, text: str) -> None:
        if not text:
            return
        if self.cfg.terminal_single_line and "\n" in text and in_terminal():
            lines = text.count("\n") + 1
            text = " ".join(part.strip() for part in text.splitlines() if part.strip())
            log.info("joined %d lines into one for the terminal", lines)
        if self.cfg.method == "type":
            self._type(text)
            return

        try:
            self._paste(text)
            log.info("pasted %d chars into %s", len(text), _program())
        except Exception as exc:
            log.warning("paste failed (%s)", exc)
            if self.cfg.clipboard_fallback:
                try:
                    set_clipboard_text(text)
                    log.info("text left on clipboard - press Ctrl+V to insert")
                    return
                except Exception:
                    pass
            raise

    def _type(self, text: str) -> None:
        kb = self.keyboard
        for ch in text:
            kb.type(ch)
            if self.cfg.type_interval:
                time.sleep(self.cfg.type_interval)

    def _paste(self, text: str) -> None:
        from pynput.keyboard import Key

        previous = None
        if self.cfg.restore_clipboard:
            try:
                previous = get_clipboard_text()
            except InjectError:
                log.debug("could not read clipboard; skipping restore")

        set_clipboard_text(text)
        # The target app needs a beat to observe the new clipboard contents
        # before it services the Ctrl+V, or it pastes the previous value.
        time.sleep(self.cfg.paste_delay)

        kb = self.keyboard
        with kb.pressed(Key.ctrl):
            kb.press("v")
            kb.release("v")
        # Windows Terminal asks before pasting over 5 KiB (or several lines,
        # if they weren't joined): dictating it was the yes.
        if len(text.encode("utf-8")) > 4000 or "\n" in text:
            from .desktop import confirm_terminal_paste

            confirm_terminal_paste()

        if self.cfg.restore_clipboard and previous is not None:
            # Restoring too eagerly races the paste for the same reason.
            time.sleep(max(self.cfg.paste_delay, 0.15))
            try:
                set_clipboard_text(previous)
            except InjectError:
                log.debug("could not restore clipboard")
