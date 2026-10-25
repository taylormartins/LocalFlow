"""Render every pixel theme (recording with live text, processing, done, a
notice) into one PNG, from the app's own draw functions. The test window is
topmost and only its own rectangle is grabbed.

    uv run python tools/shot_pixel.py out.png [scale]
"""
import ctypes
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
ctypes.windll.shcore.SetProcessDpiAwareness(2)

from ctypes import wintypes  # noqa: E402

from PIL import Image  # noqa: E402

from localflow import pixel, themes  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else "pixel.png"
s = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
LIVE = (["SEND", "SAM", "THE", "BUDGET"], ["ON", "FRIDAY", "AND", "COPY", "JO"], False)
W = int((pixel.PILL_U + 2) * pixel.cell(s))
ROW = (pixel.TEXT_U + 4 + pixel.ROW_U) * pixel.cell(s)

root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
keys = list(themes.PIXEL)
H = ROW * 3 + 30
c = tk.Canvas(root, width=W * 2 + 40, height=H * len(keys) // 2 + 30, bg="#303030",
              highlightthickness=0)
c.pack()
root.geometry("+40+40")
root.update()
now = 12.7
levels = themes.preview_levels(3.1, 28)
for n, key in enumerate(keys):
    th = themes.PIXEL[key]
    x = 10 + (n % 2) * (W + 20)
    y = 10 + (n // 2) * (ROW * 3 + 20)
    pixel.draw(c, th, x, y + ROW, W, s, now, "recording", levels, "llm", 7.0, live=LIVE)
    pixel.draw(c, th, x, y + ROW * 2 - 30, W, s, now, "busy", label="CLEANING")
    pixel.draw(c, th, x, y + ROW * 3 - 60, W, s, now, "done", label="INSERTED 64 CHARS",
               colour=th.accent)
root.update()
time.sleep(0.4)
root.update()


def grab(hwnd, w, h):
    """The window's own pixels (PrintWindow), never the desktop behind it."""
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


hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
img = grab(hwnd, c.winfo_width(), c.winfo_height())
img.save(out)
print(out, img.size)
root.destroy()
