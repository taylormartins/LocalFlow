"""The app's icon: a golden wind. Three gusts, each ending in a curl.

Drawn with Pillow at 4x and scaled down, so it stays smooth at tray size.
A small dot in the corner shows what LocalFlow is doing (none when idle); a
ring round it means it's recording.
"""

from __future__ import annotations

import logging

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

GOLD = (232, 191, 72, 255)
GOLD_LIGHT = (250, 222, 130, 255)
SHADOW = (58, 42, 8, 255)
BIG = 256


def _gust(d: ImageDraw.ImageDraw, x0: float, x1: float, y: float, r: float, up: bool,
          colour, width: int) -> None:
    """A straight run ending in a curl (up and back, or down and back)."""
    d.line((x0, y, x1, y), fill=colour, width=width)
    if up:
        box = (x1 - r, y - 2 * r, x1 + r, y)
        d.arc(box, start=180, end=90, fill=colour, width=width)
    else:
        box = (x1 - r, y, x1 + r, y + 2 * r)
        d.arc(box, start=270, end=180, fill=colour, width=width)
    h = width / 2  # round the open end
    d.ellipse((x0 - h, y - h, x0 + h, y + h), fill=colour)


GUSTS = [  # (start x, end x, y, curl radius, curls up?)
    (34, 150, 92, 26, True),
    (16, 196, 140, 28, True),
    (48, 136, 188, 24, False),
]


def golden_wind(dot: tuple[int, int, int] | None = None, ring: bool = False,
                size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (BIG, BIG), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # A dark edge first, so the gold reads on a light taskbar too.
    for colour, width in ((SHADOW, 30), (GOLD, 20)):
        for g in GUSTS:
            _gust(d, *g, colour=colour, width=width)
    for x0, x1, y, r, up in GUSTS:  # a highlight along each gust
        d.line((x0 + 8, y - 4, x1 - 6, y - 4), fill=GOLD_LIGHT, width=5)
    if dot is not None:
        cx, cy, r = 206, 206, 40
        if ring:
            d.ellipse((cx - r - 12, cy - r - 12, cx + r + 12, cy + r + 12), outline=dot + (255,),
                      width=12)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=dot + (255,), outline=SHADOW, width=7)
    return img.resize((size, size), Image.LANCZOS)


def set_app_identity() -> None:
    """Make notifications and taskbar buttons say APP_NAME with our icon
    instead of "Python" (every LocalFlow process is pythonw.exe).
    Windows names a tray balloon after the process's app ID; registering one
    (per user, no admin) with a display name and an icon does it. Must run
    before the first window or tray icon is made."""
    import ctypes
    import winreg

    from . import APP_ID, APP_NAME
    from .config import PROJECT_ROOT

    try:
        ico = PROJECT_ROOT / "data" / "app.ico"
        if not ico.exists():
            ico.parent.mkdir(parents=True, exist_ok=True)
            golden_wind(size=256).save(ico, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(ico))
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        log.warning("could not set the app's name for notifications", exc_info=True)


def window_setup(root, what: str) -> None:
    """A LocalFlow window: "<APP_NAME> - <what>" and the golden wind icon."""
    from . import APP_NAME

    root.title(f"{APP_NAME} - {what}")
    try:
        from PIL import ImageTk

        root._icon = ImageTk.PhotoImage(golden_wind(size=64))  # kept: Tk doesn't hold it
        root.iconphoto(True, root._icon)
    except Exception:
        log.debug("window icon failed", exc_info=True)
