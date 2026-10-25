"""Render the Settings window (its own pixels via PrintWindow; no config is
written) in a pixel look, on a chosen tab.

    uv run python tools/shot_settings.py out.png [look|legacy] [tab index]
"""
import ctypes
import sys
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
ctypes.windll.shcore.SetProcessDpiAwareness(2)

from PIL import Image  # noqa: E402

from localflow import settings  # noqa: E402
from localflow.config import load  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else "settings.png"
look = sys.argv[2] if len(sys.argv) > 2 else "block"
tab = int(sys.argv[3]) if len(sys.argv) > 3 else 0
cfg = load()[0]
cfg.overlay.pixel = look != "legacy"
cfg.overlay.pixel_theme = look if look != "legacy" else "block"
settings._save = lambda *a, **k: None


def grab(hwnd, w, h):
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    sdc = user32.GetDC(0)
    dc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, w, h)
    gdi32.SelectObject(dc, bmp)
    user32.PrintWindow(hwnd, dc, 2)

    class BIH(ctypes.Structure):
        _fields_ = [("s", wintypes.DWORD), ("w", wintypes.LONG), ("h", wintypes.LONG),
                    ("p", wintypes.WORD), ("b", wintypes.WORD), ("c", wintypes.DWORD),
                    ("si", wintypes.DWORD), ("x", wintypes.LONG), ("y", wintypes.LONG),
                    ("u", wintypes.DWORD), ("i", wintypes.DWORD)]

    bi = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(0, sdc)
    return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")


def fake_mainloop(self):
    from tkinter import ttk

    for _ in range(12):
        self.update()
        self.after(60)
    nb = [w for w in self.winfo_children() if isinstance(w, ttk.Notebook)][0]
    nb.select(tab)
    for _ in range(12):
        self.update()
        self.after(60)
    hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
    grab(hwnd, self.winfo_width(), self.winfo_height()).save(out)
    print(out)
    self.destroy()


tk.Tk.mainloop = fake_mainloop
settings.main(cfg)
