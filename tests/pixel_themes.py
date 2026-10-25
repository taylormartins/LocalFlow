"""The pixel themes (overlay.pixel): which theme is in use, the ticker back to
the legacy v2 themes, and the REAL overlay window drawn with each one.

The overlay is captured with PrintWindow (the window's own pixels, never the
desktop behind it). Control: a legacy theme must NOT show a pixel look's
border colours, so the checks can't pass on any picture. Pass an output
directory to keep the PNGs.

    uv run python tests/pixel_themes.py [outdir]
"""
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
ctypes.windll.shcore.SetProcessDpiAwareness(2)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from localflow import pixel, themes  # noqa: E402
from localflow.config import Config, OverlayConfig  # noqa: E402
from localflow.engine import Engine  # noqa: E402
from localflow.overlay import Overlay  # noqa: E402

failures = 0
user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else None


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


def capture(hwnd) -> Image.Image:
    """The window's own pixels through PrintWindow(PW_RENDERFULLCONTENT)."""
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
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
    arr = np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, [2, 1, 0]]
    return Image.fromarray(arr.copy(), "RGB")


def has(img: Image.Image, colour: str, at_least: int = 6) -> bool:
    rgb = np.array(pixel.hexrgb(colour))
    return int((np.asarray(img) == rgb).all(axis=2).sum()) >= at_least


print("which theme is in use")
ov = OverlayConfig()
expect("legacy is the default look", not ov.pixel and themes.effective(ov) == "royal")
ov.pixel = True
expect("pixel when ticked", themes.effective(ov) == "pixel-block")
ov.pixel_theme = "wizard"
expect("a pixel pick", themes.effective(ov) == "pixel-wizard")
ov.pixel_theme = "nonsense"
expect("unknown falls back to block", themes.effective(ov) == "pixel-block")
ov.pixel = False
ov.theme = "gemstone"
expect("the ticker off returns to the legacy theme", themes.effective(ov) == "gemstone")
expect("legacy keeps its own pick while pixel is on", OverlayConfig().theme == "royal")
expect("legacy list has no pixel themes", not any(k.startswith("pixel-") for k in themes.legacy_themes()))
expect("twelve pixel themes", sorted(themes.PIXEL) == sorted(f"pixel-{k}" for k in (
    "block", "royal", "dark", "light", "knight", "wizard", "electric", "leaf", "dragon", "legal", "notebook", "chaos")))

print("the real overlay window, each theme")
cfg = Config()
cfg.history.enabled = False
cfg.cleanup.method = "rules"
engine = Engine(cfg)
ovl = Overlay(engine, cfg.overlay, live=None)
ovl.start()
for _ in range(100):
    if getattr(ovl, "hwnd", None):
        break
    time.sleep(0.1)
expect("the overlay window came up", bool(getattr(ovl, "hwnd", 0)))


def show(mode, label="Inserted", seconds=0.5):
    ovl._mode, ovl._label, ovl._colour = mode, label, themes.pixel.LOOKS["block"].pal["accent"]
    ovl._since = time.monotonic() - 3
    ovl._hide_at = time.monotonic() + 60
    time.sleep(seconds)


try:
    shots = {}
    for key in ["legacy"] + list(pixel.LOOKS):
        cfg.overlay.pixel = key != "legacy"
        cfg.overlay.pixel_theme = key if key != "legacy" else "block"
        show("done", "INSERTED")
        shots[key] = img = capture(ovl.hwnd)
        if OUT:
            OUT.mkdir(parents=True, exist_ok=True)
            img.save(OUT / f"overlay_{key}.png")
    ring = {k: pixel.LOOKS[k].pal["shadow"] for k in pixel.LOOKS}
    for key, look in pixel.LOOKS.items():
        expect(f"{key}: its ring and accent are on screen",
               has(shots[key], look.pal["shadow"]) and has(shots[key], look.pal["accent"]))
        expect(f"{key}: nothing is anti-aliased (few distinct colours)",
               len({tuple(c) for c in np.asarray(shots[key]).reshape(-1, 3)}) < 40,
               str(len({tuple(c) for c in np.asarray(shots[key]).reshape(-1, 3)})))
    expect("CONTROL legacy v2 draws none of the pixel rings",
           not any(has(shots["legacy"], c, 30) for c in ring.values()))
    expect("legacy still draws (not blank)", len({tuple(c) for c in
           np.asarray(shots["legacy"]).reshape(-1, 3)}) > 3)
    # recording with live words: two lines of text above the meter, taller window area used
    from types import SimpleNamespace

    ovl.live = SimpleNamespace(text="send sam the budget on friday and copy jo on the notes", version=1)
    for key in pixel.LOOKS:
        cfg.overlay.pixel_theme = key
        ovl._mode, ovl._since, ovl._hide_at = "recording", time.monotonic() - 5, time.monotonic() + 60
        time.sleep(0.5)
        img = capture(ovl.hwnd)
        if OUT:
            img.save(OUT / f"overlay_{key}_live.png")
        txt = pixel.LOOKS[key].pal["text"]
        rows = np.where((np.asarray(img) == np.array(pixel.hexrgb(txt))).all(axis=2).any(axis=1))[0]
        expect(f"{key}: live words drawn above the meter (text on two bands of rows)",
               len(rows) > 10 and rows.min() < img.height * 0.5, f"{len(rows)} rows")
    ovl.live = None
    # the notice mark: a tick for ok, a bang for an error
    cfg.overlay.pixel, cfg.overlay.pixel_theme = True, "knight"
    ovl._mode, ovl._glyph = "notice", "\u2713"
    ovl._label, ovl._since, ovl._hide_at = "THEME: PIXEL KNIGHT", time.monotonic() - 3, time.monotonic() + 60
    time.sleep(0.4)
    expect("a notice draws", has(capture(ovl.hwnd), pixel.LOOKS["knight"].pal["text"]))
finally:
    ovl.quit()
sys.exit(1 if failures else 0)
