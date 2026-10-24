"""The pill must come back when another topmost window covers it.

Real Win32 windows: a pill-style layer and a second topmost window raised over
it. Control: without _keep_on_top the pill stays buried; with it, it surfaces.
Run: uv run python tests/pill_stays_on_top.py
"""
import ctypes
import sys
import time
import tkinter as tk
from types import SimpleNamespace

from localflow import layers, overlay

user32 = ctypes.windll.user32
GW_HWNDPREV = 3


def covered(hwnd) -> bool:
    """Is any visible window above it that overlaps it? (Other apps' tiny
    topmost helper windows elsewhere on screen don't count.)"""
    from ctypes import wintypes

    mine, other = wintypes.RECT(), wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(mine))
    above = user32.GetWindow(hwnd, GW_HWNDPREV)
    while above:
        if user32.IsWindowVisible(above):
            user32.GetWindowRect(above, ctypes.byref(other))
            if (other.left < mine.right and other.right > mine.left
                    and other.top < mine.bottom and other.bottom > mine.top):
                return True
        above = user32.GetWindow(above, GW_HWNDPREV)
    return False


def main() -> int:
    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry("40x20+-32000+-32000")
    root.update()
    pill = layers.prepare(root)
    layers.show(pill, 200, 200)

    other = tk.Toplevel(root)
    other.overrideredirect(True)
    other.geometry("60x60+190+190")
    other.attributes("-topmost", True)
    other.update()
    layers.raise_(user32.GetParent(other.winfo_id()))  # the intruder on top
    root.update()

    fake = SimpleNamespace(hwnd=pill, visible=True, _next_raise=0.0, _show=lambda: None)
    buried = covered(pill)
    print("buried by the other topmost window:", buried)

    # Control: do nothing, the pill must still be buried.
    time.sleep(0.6)
    control_ok = covered(pill)

    overlay.Overlay._keep_on_top(fake)
    root.update()
    fixed = not covered(pill)

    # Hidden by the system -> shown again.
    shown = []
    fake2 = SimpleNamespace(hwnd=pill, visible=True, _next_raise=0.0,
                            _show=lambda: shown.append(1))
    user32.ShowWindow(pill, 0)
    overlay.Overlay._keep_on_top(fake2)

    other.destroy()
    root.destroy()
    print("control stays buried:", control_ok, "| surfaces with fix:", fixed,
          "| re-shown after hide:", bool(shown))
    return 0 if (buried and control_ok and fixed and shown) else 1


if __name__ == "__main__":
    sys.exit(main())
