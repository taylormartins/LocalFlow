"""The app's icon: a golden crown lifted by the golden wind.

Two drawings. The pixel one (always; house style in
C:/Code/design) is a 28x28 hand-placed sprite scaled with nearest-neighbour,
the same picture for the tray, the windows, the notification icon and the
header. The legacy one is drawn with Pillow at 4x and scaled down smooth.
A small mark in the corner shows what LocalFlow is doing (none when idle); a
ring round it means it's recording.
"""

from __future__ import annotations

import io
import logging
import struct

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


RUBY = (214, 36, 72, 255)
#: The crown: band corners and its three points (left, middle, right).
CROWN = [(46, 150), (30, 62), (84, 106), (128, 34), (172, 106), (226, 62), (210, 150)]
TIPS = [(30, 62), (128, 34), (226, 62)]
#: The wind that carries it: two swooshes curving up under the crown
#: (ellipse box, start and end angle; Pillow measures clockwise from 3 o'clock).
SWOOSH = [((6, 70, 250, 238), 18, 150), ((40, 120, 226, 250), 30, 128)]



def pixel_on() -> bool:
    """The icon is always the pixel crown (the pixel themes are an opt-in for
    the pill; the emblem is not). Kept as a function: callers ask it."""
    return True


# The pixel crown, on a 28x28 grid: no anti-aliasing, a dark tile behind it.
PX_TILE, PX_EDGE, PX_EDGE2 = (28, 27, 32), (139, 106, 69), (64, 54, 52)
PX_GOLD, PX_LIGHT, PX_SHADE, PX_DARK = (240, 197, 121), (252, 232, 170), (176, 130, 52), (58, 42, 8)
PX_RUBY = (222, 44, 80)


def pixel_crown(dot: tuple[int, int, int] | None = None, ring: bool = False,
                size: int = 64) -> Image.Image:
    """The golden crown lifted by the golden wind, as a 28x28 pixel sprite."""
    im = Image.new("RGBA", (28, 28), PX_TILE + (255,))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 27, 27), outline=PX_EDGE)
    d.rectangle((1, 1, 26, 26), outline=PX_EDGE2)
    # the wind: two gusts curling up under the crown
    for y, x0, x1, col in ((24, 5, 19, PX_SHADE), (22, 8, 22, PX_GOLD)):
        d.line((x0, y, x1, y), fill=col)
    d.line((19, 24, 21, 24), fill=PX_SHADE)
    d.point((22, 23), fill=PX_SHADE)
    d.point((23, 22), fill=PX_GOLD)
    d.point((23, 21), fill=PX_LIGHT)
    d.line((4, 25, 11, 25), fill=PX_GOLD)
    # the crown: dark edge first, then gold
    body = [(6, 18), (5, 8), (10, 12), (14, 5), (18, 12), (23, 8), (22, 18)]
    d.polygon(body, fill=PX_GOLD, outline=PX_DARK)
    d.rectangle((6, 16, 22, 20), fill=PX_GOLD, outline=PX_DARK)
    d.line((7, 17, 21, 17), fill=PX_LIGHT)
    d.line((7, 19, 21, 19), fill=PX_SHADE)
    for x, y in ((5, 7), (14, 4), (23, 7)):  # a bead on each point
        d.rectangle((x - 1, y - 1, x, y), fill=PX_LIGHT, outline=PX_DARK)
    d.rectangle((13, 17, 14, 19), fill=PX_RUBY)
    d.point((13, 17), fill=(255, 150, 170))
    for x in (9, 18):
        d.point((x, 14), fill=PX_LIGHT)
    if dot is not None:
        c = dot + (255,)
        if ring:
            d.rectangle((17, 17, 27, 27), outline=c)
        d.rectangle((19, 19, 25, 25), fill=c, outline=PX_DARK)
        d.rectangle((20, 20, 21, 21), fill=(255, 255, 255, 255))
    return im.resize((size, size), Image.NEAREST)


def save_ico(path, size_list=(16, 24, 32, 48, 64, 128, 256)) -> None:
    """An .ico of the pixel crown with every size scaled nearest-neighbour
    (Pillow's own ICO writer would smooth them)."""
    base = pixel_crown(size=28)
    layers = []
    for n in size_list:
        b = io.BytesIO()
        base.resize((n, n), Image.NEAREST).save(b, "PNG")
        layers.append(b.getvalue())
    off = 6 + 16 * len(layers)
    out = struct.pack("<HHH", 0, 1, len(layers))
    for n, blob in zip(size_list, layers):
        out += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(blob), off)
        off += len(blob)
    with open(path, "wb") as f:
        f.write(out + b"".join(layers))


def golden_wind(dot: tuple[int, int, int] | None = None, ring: bool = False,
                size: int = 64) -> Image.Image:
    """The emblem: a golden crown lifted by the golden wind (taking command
    of the computer). The name is kept: everything that draws the icon
    calls this. Always the pixel crown; the smooth one is kept."""
    if pixel_on():
        return pixel_crown(dot, ring, size)
    return smooth_crown(dot, ring, size)


def smooth_crown(dot: tuple[int, int, int] | None = None, ring: bool = False,
                 size: int = 64) -> Image.Image:
    """The legacy v2 crown (smooth)."""
    img = Image.new("RGBA", (BIG, BIG), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # A dark edge first, so the gold reads on a light taskbar too.
    d.polygon(CROWN, fill=SHADOW, outline=SHADOW, width=12)
    for x, y in TIPS:
        d.ellipse((x - 22, y - 22, x + 22, y + 22), fill=SHADOW)
    d.rectangle((40, 146, 216, 186), fill=SHADOW)
    for box, start, end in SWOOSH:
        d.arc(box, start, end, fill=SHADOW, width=30)
    # Gold: the wind first, so the crown sits in front of it.
    for k, (box, start, end) in enumerate(SWOOSH):
        d.arc(box, start, end, fill=GOLD if k == 0 else GOLD_LIGHT, width=20)
    d.polygon(CROWN, fill=GOLD, outline=SHADOW, width=6)
    for x, y in TIPS:
        d.ellipse((x - 14, y - 14, x + 14, y + 14), fill=GOLD_LIGHT)
    d.rectangle((46, 150, 210, 182), fill=GOLD, outline=SHADOW, width=5)
    d.line((52, 156, 204, 156), fill=GOLD_LIGHT, width=5)  # light on the band
    d.polygon([(128, 150), (140, 166), (128, 182), (116, 166)], fill=RUBY)  # a jewel
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
    import sys

    if sys.platform != "win32":
        return  # a Mac names the app after its process; nothing to register
    import ctypes
    import winreg

    from . import APP_ID, APP_NAME
    from .config import PROJECT_ROOT

    try:
        pix = pixel_on()
        ico = PROJECT_ROOT / "data" / ("app-crown-px.ico" if pix else "app-crown.ico")
        if not ico.exists():
            ico.parent.mkdir(parents=True, exist_ok=True)
            if pix:
                save_ico(ico)
            else:
                smooth_crown(size=256).save(ico, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
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
