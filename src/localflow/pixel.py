"""Pixel themes: the owner's pixel house style (the design standards live outside this repo).

Everything is drawn on a grid of "art pixels" (one art pixel = ``cell`` screen
pixels, no anti-aliasing anywhere): Silkscreen type (8 px, all caps), square
beveled shapes, a chunky segmented border that animates in hard steps, LED-ish
level bars, hand-made sprites. Four looks share one renderer and differ in
palette, border, recording mark and meter:

- block:    the castle palette; gold segments, an orb, LED bars. The plain one.
- knight:   medieval stone with battlements, steel and gold, a shield, blades.
- wizard:   arcane violet, sparkles popping on the border, a star, runes.
- electric: yellow and cyan on near-black, flickering segments, a bolt.

Pixels are put on the Tk canvas as small cached PNG images (the border body,
text, sprites) and plain rectangles (the meter), so a frame is a few dozen
canvas items. Nothing here imports themes.py: themes.py imports this.

The legacy (smooth) themes stay in themes.py; Settings > Appearance has a
ticker between the two (overlay.pixel).
"""

from __future__ import annotations

import base64
import io
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

ASSETS = Path(__file__).parent / "assets"
FONT_FILE = ASSETS / "Silkscreen-Regular.ttf"
#: The Tk family name once the font is registered (for the other layers).
FAMILY = "Silkscreen"

#: The pill row is this many art pixels tall, and wide by default.
ROW_U = 14
PILL_U = 76
#: Caps are 5 art pixels tall; a line of live text is 5 + 3 apart.
LINE_U = 7
CAP_U = 5
#: Two lines of live text above the pill row, with padding.
#: Rows between the top edge and the first line of live text: the border is 2
#: rows, the bevel 1, the knight's merlons dip one more, and a row of air.
TEXT_PAD = 5
#: Rows cut from the gap between the last line of text and the pill row.
TEXT_TAIL = 2
#: Looks whose outline rises above the pill (flames, crown points): rows each uses.
CRESTS = {"flame": 4, "royal": 5, "prism": 3, "wave": 5}
CREST_U = max(CRESTS.values())
TEXT_U = 2 * LINE_U + TEXT_PAD - TEXT_TAIL + 2 + CREST_U

#: What the recording mark's colour means; the same in every pixel look
#: (chrome is themed, data keeps its true colour).
DOTS = {"llm": "#4bd36b", "rules": "#4aa3ff", "raw": "#f4f4f4"}
RED = "#ef5350"


def cell(s: float) -> int:
    """Screen pixels per art pixel at this scale."""
    return max(2, round(1.9 * s))


def hexrgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def tohex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in rgb)


def mix(a: str, b: str, t: float) -> str:
    ra, rb = hexrgb(a), hexrgb(b)
    return tohex([x + (y - x) * t for x, y in zip(ra, rb)])


# -- the looks -------------------------------------------------------------------

@dataclass(frozen=True)
class Look:
    key: str
    title: str
    blurb: str
    pal: dict
    border: str      # block | dark | ink | swirl | prism | wave | stone | arcane | spark | leaf | flame | legal | royal
    dot: str         # orb | shield | star | bolt | leaf | flame | dragon | scales | crown | moon | sun
    wave: str        # led | blade | rune | bolt | vine | flame | stack | iris | gems | bar
    sparks: tuple = field(default_factory=tuple)


_ROLES = ("canvas", "panel", "raised", "line", "shadow", "hi", "accent", "accentdark", "text",
          "muted", "accent2")


def _pal(**kw) -> dict:
    assert set(kw) == set(_ROLES), set(_ROLES) ^ set(kw)
    return kw


LOOKS: dict[str, Look] = {
    "block": Look(
        "block", "PIXEL BLOCK", "THE PLAIN ONE: CASTLE GOLD",
        _pal(canvas="#1c1b20", panel="#302a2b", raised="#403634", line="#6d5540", shadow="#101014",
             hi="#8b6a45", accent="#f0c579", accentdark="#52402a", text="#f4e8d1", muted="#baaa92",
             accent2="#f0c579"),
        "block", "orb", "led", ("#f0c579", "#f4e8d1")),
    "royal": Look(
        "royal", "PIXEL ROYAL", "A CROWN OF GOLD",
        _pal(canvas="#1a0f26", panel="#2a1840", raised="#3d2460", line="#8a6d1f", shadow="#0a0512",
             hi="#c9a23a", accent="#ffd24a", accentdark="#5a4410", text="#fff3d6", muted="#c9b3e6",
             accent2="#fff0a0"),
        "royal", "crown", "gems", ("#ffd24a", "#ffffff", "#fff0a0")),
    "dark": Look(
        "dark", "PIXEL DARK", "ALL THE LIGHTS OFF",
        _pal(canvas="#0c0c0f", panel="#16161b", raised="#202028", line="#3c3c48", shadow="#040405",
             hi="#707080", accent="#c8cad8", accentdark="#33333d", text="#e8e8ee", muted="#8c8c9c",
             accent2="#9aa0ff"),
        "dark", "moon", "bar", ("#c8cad8", "#ffffff")),
    "light": Look(
        "light", "PIXEL LIGHT", "DAYLIGHT",
        _pal(canvas="#f3eee2", panel="#e6dfcf", raised="#d9d0bb", line="#b7a98a", shadow="#4a4234",
             hi="#ffffff", accent="#e6a010", accentdark="#f0d79a", text="#2a2520", muted="#6f6656",
             accent2="#d9572b"),
        "block", "sun", "iris", ("#e6a010", "#d9572b")),
    "knight": Look(
        "knight", "PIXEL KNIGHT", "STONE, STEEL AND GOLD",
        _pal(canvas="#15171d", panel="#22262f", raised="#2f3440", line="#5f6878", shadow="#0a0b0e",
             hi="#9aa5b8", accent="#e8c04a", accentdark="#4a3d16", text="#eef1f7", muted="#a4adbd",
             accent2="#c8323c"),
        "stone", "shield", "blade", ("#e8c04a", "#eef1f7")),
    "wizard": Look(
        "wizard", "PIXEL WIZARD", "CAST A SPELL",
        _pal(canvas="#130d26", panel="#1e1540", raised="#2c205c", line="#5b45b0", shadow="#08051a",
             hi="#8f78e6", accent="#c8a6ff", accentdark="#3a2a78", text="#f0e8ff", muted="#b4a2e0",
             accent2="#4ff0ff"),
        "arcane", "star", "rune", ("#4ff0ff", "#ffe58a", "#ffffff")),
    "electric": Look(
        "electric", "PIXEL ELECTRIC", "LIVE WIRE",
        _pal(canvas="#080a12", panel="#111627", raised="#1a2140", line="#3b4f8c", shadow="#03040a",
             hi="#6f8cff", accent="#ffe81a", accentdark="#4a4200", text="#ffffff", muted="#9fb4e8",
             accent2="#2de6ff"),
        "spark", "bolt", "bolt", ("#ffffff", "#ffe81a", "#2de6ff")),
    "leaf": Look(
        "leaf", "PIXEL LEAF", "LEAF STORM",
        _pal(canvas="#0f1a12", panel="#17281b", raised="#21381f", line="#3f7a3a", shadow="#060c07",
             hi="#7bc96a", accent="#8fe05a", accentdark="#254019", text="#eaf8dc", muted="#a8c79a",
             accent2="#d4f06a"),
        "leaf", "leaf", "vine", ("#8fe05a", "#d4f06a", "#eaf8dc")),
    "dragon": Look(
        "dragon", "PIXEL DRAGON", "DRAGON FIRE",
        _pal(canvas="#120a22", panel="#1d1038", raised="#2c1a55", line="#5a3aa8", shadow="#07030f",
             hi="#8a6cf0", accent="#7ad7ff", accentdark="#2a1a60", text="#f0eaff", muted="#b6a6e6",
             accent2="#c050ff"),
        "flame", "dragon", "flame", ("#7ad7ff", "#c050ff", "#ff5ad8", "#4ff0ff", "#8a6cf0")),
    "legal": Look(
        "legal", "PIXEL LEGAL", "THE COURT IS IN SESSION",
        _pal(canvas="#121a26", panel="#1b2636", raised="#273649", line="#4d5d78", shadow="#070b12",
             hi="#8f9db8", accent="#b89b5e", accentdark="#3e3214", text="#ece6d6", muted="#a9a698",
             accent2="#8f9db8"),
        "legal", "scales", "stack", ("#b89b5e", "#ece6d6", "#ffffff")),
    "notebook": Look(
        "notebook", "PIXEL NOTEBOOK", "BLACK INK ON PAPER",
        _pal(canvas="#f7f7f3", panel="#e9e9e3", raised="#d9d9d2", line="#8a8a84", shadow="#ffffff",
             hi="#ffffff", accent="#111111", accentdark="#c9c9c2", text="#111111", muted="#55554f",
             accent2="#333333"),
        "ink", "pen", "scribble", ("#111111", "#555555")),
    "chaos": Look(
        "chaos", "PIXEL CHAOS", "DOWN THE BLACK HOLE",
        _pal(canvas="#08030f", panel="#150a26", raised="#25103f", line="#5a22a8", shadow="#030107",
             hi="#b07cff", accent="#a85cff", accentdark="#2e0f5c", text="#f3e8ff", muted="#b79be6",
             accent2="#e8c4ff"),
        "swirl", "hole", "swirl", ("#a85cff", "#e8c4ff", "#6a2fd0", "#ffffff")),
    "diamond": Look(
        "diamond", "PIXEL DIAMOND", "ICE ALL OVER IT",
        _pal(canvas="#0a1018", panel="#14202e", raised="#1f3045", line="#7fa8d0", shadow="#04080e",
             hi="#e8f6ff", accent="#bfe8ff", accentdark="#2a4766", text="#ffffff", muted="#9fc4e6",
             accent2="#ffffff"),
        "prism", "gem", "prism", ("#ffffff", "#d6e6ff", "#ffd6f0", "#d6fff0", "#fff6d6")),
    "fire": Look(
        "fire", "PIXEL FIRE", "BURN IT ALL DOWN",
        _pal(canvas="#140605", panel="#26100a", raised="#3a1a0e", line="#9a3a12", shadow="#080201",
             hi="#ff9a3c", accent="#ff7a1a", accentdark="#5a1a08", text="#fff1de", muted="#e0a878",
             accent2="#ffd23a"),
        "flame", "flame", "flame", ("#ff3b1a", "#ff8a1a", "#ffc21a", "#fff08a", "#ff5a1a")),
    "water": Look(
        "water", "PIXEL WATER", "THE GREAT WAVE",
        _pal(canvas="#06121f", panel="#0c2236", raised="#143555", line="#2a6a9a", shadow="#030a12",
             hi="#9fd0e8", accent="#4fa3d8", accentdark="#12324e", text="#eaf6ff", muted="#8fb8d4",
             accent2="#f4efe0"),
        "wave", "wave", "surf", ("#4fa3d8", "#f4efe0", "#9fd0e8")),
}


# -- font and image cache ----------------------------------------------------------

_font = None
_registered = False


def register_font() -> None:
    """Make Silkscreen a font family Tk can use (for the label layers). The pill
    itself draws text from the same file through PIL, so it never needs this."""
    global _registered
    if _registered or not sys.platform.startswith("win"):
        return
    _registered = True
    try:
        import ctypes

        n = ctypes.windll.gdi32.AddFontResourceExW(str(FONT_FILE), 0x10, 0)  # FR_PRIVATE
        if not n:
            log.warning("could not register %s", FONT_FILE.name)
    except Exception:
        log.warning("could not register %s", FONT_FILE.name, exc_info=True)


def tk_family() -> str:
    """The Tk family for the Silkscreen text on the other layers."""
    register_font()
    return FAMILY if sys.platform.startswith("win") else "Courier"


def pixel_px(px: float) -> int:
    """A Tk font size in pixels as a whole multiple of Silkscreen's 8 (so
    its pixels stay square): 13 -> 16, 26 -> 24."""
    return max(8, int(round(px / 8.0)) * 8)


def _pil_font():
    global _font
    if _font is None:
        _font = ImageFont.truetype(str(FONT_FILE), 8)
    return _font


def text_w(s: str) -> int:
    """Width of the text in art pixels."""
    return int(_pil_font().getlength(s.upper()))


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue())


def _photo(c, key, make):
    cache = getattr(c, "_pxl_cache", None)
    if cache is None:
        cache = c._pxl_cache = {}
    ph = cache.get(key)
    if ph is None:
        import tkinter as tk

        if len(cache) > 900:
            cache.clear()
        ph = tk.PhotoImage(master=c, data=_png(make()), format="png")
        cache[key] = ph
    return ph


def _scaled(arr: np.ndarray, k: int) -> Image.Image:
    img = Image.fromarray(arr, "RGBA")
    return img.resize((img.width * k, img.height * k), Image.NEAREST) if k != 1 else img


def _text_array(s: str, colour: str) -> np.ndarray:
    w = max(1, text_w(s) + 1)
    m = Image.new("L", (w, 7), 0)
    d = ImageDraw.Draw(m)
    d.fontmode = "1"
    d.text((0, -4), s, font=_pil_font(), fill=255)
    on = np.asarray(m) > 0
    arr = np.zeros((7, w, 4), np.uint8)
    arr[on] = (*hexrgb(colour), 255)
    return arr


class Grid:
    """Art-pixel coordinates over a canvas: (ox, oy) is the screen position of
    art pixel (0, 0), ``k`` screen pixels per art pixel."""

    def __init__(self, c, ox: float, oy: float, k: int):
        self.c, self.ox, self.oy, self.k = c, int(ox), int(oy), k

    def rect(self, x: int, y: int, w: int, h: int, colour: str) -> None:
        if w <= 0 or h <= 0:
            return
        k = self.k
        self.c.create_rectangle(self.ox + x * k, self.oy + y * k, self.ox + (x + w) * k,
                                self.oy + (y + h) * k, fill=colour, outline="")

    def text(self, x: int, y: int, s: str, colour: str, scale: int = 1, anchor: str = "w") -> int:
        """Caps-only text; (x, y) is the left / right / centre of the first
        row of caps. Returns its width in art pixels."""
        s = s.upper()
        if not s.strip():
            return 0
        w = text_w(s)
        if anchor == "e":
            x -= w * scale
        elif anchor == "c":
            x -= (w * scale) // 2
        k = self.k * scale
        ph = _photo(self.c, ("t", s, colour, k), lambda: _scaled(_text_array(s, colour), k))
        self.c.create_image(self.ox + x * self.k, self.oy + y * self.k, image=ph, anchor="nw")
        return w * scale

    def sprite(self, x: int, y: int, rows: list[str], pal: dict) -> None:
        key = ("s", tuple(rows), tuple(sorted(pal.items())), self.k)

        def make():
            arr = np.zeros((len(rows), max(len(r) for r in rows), 4), np.uint8)
            for j, row in enumerate(rows):
                for i, ch in enumerate(row):
                    if ch in pal:
                        arr[j, i] = (*hexrgb(pal[ch]), 255)
            return _scaled(arr, self.k)

        ph = _photo(self.c, key, make)
        self.c.create_image(self.ox + x * self.k, self.oy + y * self.k, image=ph, anchor="nw")


# -- the body and its segmented border -----------------------------------------------

def _erode(m: np.ndarray) -> np.ndarray:
    e = m.copy()
    e[1:, :] &= m[:-1, :]
    e[:-1, :] &= m[1:, :]
    e[:, 1:] &= m[:, :-1]
    e[:, :-1] &= m[:, 1:]
    e[0, :] = e[-1, :] = False
    e[:, 0] = e[:, -1] = False
    return e


def _shape(wu: int, hu: int, battlements: bool) -> np.ndarray:
    """The pill: a rectangle with stepped corners (a 3-pixel notch), and for
    the knight merlons (one row deep) along the top."""
    m = np.ones((hu, wu), bool)
    if battlements and hu > 8:
        for x in range(wu):
            if (x // 4) % 2 == 1:
                m[:1, x] = False
    for dx, dy in ((0, 0), (1, 0), (0, 1)):
        m[dy, dx] = m[dy, wu - 1 - dx] = False
        m[hu - 1 - dy, dx] = m[hu - 1 - dy, wu - 1 - dx] = False
    return m


def _hash(a: int, b: int) -> int:
    return ((a * 73856093) ^ (b * 19349663)) % 997


def _segment_colour(look: Look, i: int, n: int, step: int) -> str:
    p = look.pal
    if look.border == "block":
        v = round((math.sin(i * 0.5 - step * 0.9) + 1) / 2 * 3) / 3
        col = mix(p["line"], p["accent"], v)
        return mix(col, p["shadow"], 0.28) if i % 2 else col
    if look.border == "dark":
        # blacks and dark greys, a slow swell running round
        v = round((math.sin(i * 0.5 - step * 0.6) + 1) / 2 * 3) / 3
        col = mix(p["shadow"], p["line"], v * 0.8)
        return mix(col, p["shadow"], 0.4) if i % 2 else col
    if look.border == "prism":
        # a faceted edge: white, with a very light rainbow in every other facet
        if _hash(i, step // 2) % 4 == 0:
            return "#ffffff"
        col = look.sparks[1 + (i + step // 2) % 4]
        return mix(col, p["shadow"], 0.3) if i % 2 else col
    if look.border == "wave":
        # water running round: blues swelling, a fleck of foam on the crests
        v = round((math.sin(i * 0.5 - step * 0.8) + 1) / 2 * 3) / 3
        if v == 1 and _hash(i, step) % 3 == 0:
            return p["accent2"]
        col = mix(p["line"], p["accent"], v)
        return mix(col, p["shadow"], 0.3) if i % 2 else col
    if look.border == "ink":
        # a pen's line: black, a few strokes pressed lighter, the nib running round
        pen = (step // 2) % n
        if i in (pen, (pen + 1) % n):
            return "#000000"
        return mix(p["accent"], p["canvas"], 0.45 if _hash(i, 3) % 5 == 0 else 0.0)
    if look.border == "swirl":
        # two waves chasing each other round the edge: a whirlpool's arms
        v = round((math.sin(i * 0.38 + step * 1.1) + math.sin(i * 0.95 - step * 0.7) + 2) / 4 * 4) / 4
        col = mix(p["shadow"], p["accent"], v / 0.75) if v <= 0.75 else p["accent2"]
        return mix(col, p["shadow"], 0.35) if i % 2 else col
    if look.border == "stone":
        # Brick: two greys, with one gold glint walking round (a torch passing).
        glint = (step // 2) % n
        if i in (glint, (glint + 1) % n):
            return p["accent"]
        col = p["hi"] if _hash(i, 3) % 5 == 0 else p["line"]
        return mix(col, p["shadow"], 0.3) if i % 2 else col
    if look.border == "arcane":
        v = round((math.sin(i * 0.45 + step * 0.8) + 1) / 2 * 3) / 3
        col = mix(p["hi"], p["accent2"], v)
        return mix(col, p["shadow"], 0.35) if i % 2 else col
    if look.border == "leaf":
        if _hash(i, step // 2) % 9 == 0:
            return p["accent2"]
        v = round((math.sin(i * 0.6 + step * 0.7) + 1) / 2 * 3) / 3
        col = mix(p["line"], p["accent"], v)
        return mix(col, p["shadow"], 0.3) if i % 2 else col
    if look.border == "flame":
        # fire: every segment is some colour of the flame, re-lit each step
        col = look.sparks[_hash(i, step) % len(look.sparks)]
        if _hash(i, step + 5) % 9 == 0:
            col = mix(col, "#ffffff", 0.65)
        return mix(col, p["shadow"], 0.35) if i % 2 else col
    if look.border == "legal":
        # a brass rule; a slow glint is the only thing that moves
        if i == (step // 3) % n:
            return mix(p["accent"], "#ffffff", 0.35)
        return mix(p["accent"], p["shadow"], 0.5 if i % 2 else 0.3)
    if look.border == "royal":
        # gold only: a shimmer running round it
        v = round((math.sin(i * 0.5 - step * 0.9) + 1) / 2 * 3) / 3
        col = mix(p["line"], p["accent"], v)
        return mix(col, p["shadow"], 0.3) if i % 2 else col
    # spark: yellow and cyan, any segment may flash white for a step
    if _hash(i, step) % 11 == 0:
        return "#ffffff"
    col = p["accent"] if i % 2 == 0 else p["accent2"]
    return mix(col, p["shadow"], 0.45) if _hash(i, step // 3) % 4 == 0 else col


_body_cache: dict = {}


def _body(look: Look, wu: int, hu: int, step: int) -> np.ndarray:
    key = (look.key, wu, hu, step)
    if key in _body_cache:
        return _body_cache[key]
    if len(_body_cache) > 160:
        _body_cache.clear()
    p = look.pal
    m = _shape(wu, hu, look.border == "stone")
    inner1 = _erode(m)
    inner2 = _erode(inner1)
    ring0, ring1 = m & ~inner1, inner1 & ~inner2
    arr = np.zeros((hu, wu, 4), np.uint8)
    arr[inner2] = (*hexrgb(p["canvas"]), 255)
    # the bevel language inside: light top, dark bottom
    up = np.zeros_like(inner2)
    up[1:] = inner2[:-1]
    dn = np.zeros_like(inner2)
    dn[:-1] = inner2[1:]
    arr[inner2 & ~up] = (*hexrgb(mix(p["canvas"], p["hi"], 0.16)), 255)
    arr[inner2 & ~dn] = (*hexrgb(mix(p["canvas"], p["shadow"], 0.55)), 255)
    arr[ring0] = (*hexrgb(p["shadow"]), 255)
    # segments run round the perimeter in blocks of ~5 art pixels
    length = 2 * (wu + hu)
    n = max(8, length // 5)
    colours = [hexrgb(_segment_colour(look, i, n, step)) for i in range(n)]
    ys, xs = np.nonzero(ring1)
    for y, x in zip(ys, xs):
        d = (x, wu - 1 - x, y, hu - 1 - y)
        side = d.index(min(d))
        t = x if side == 2 else wu + y if side == 1 else wu + hu + (wu - 1 - x) if side == 3 else \
            2 * wu + hu + (hu - 1 - y)
        arr[y, x] = (*colours[min(n - 1, t * n // length)], 255)
    _body_cache[key] = arr
    return arr


def _extras(look: Look, g: Grid, wu: int, hu: int, t: float) -> None:
    """Sparkles on the wizard's border, the electric look's lightning."""
    step = int(t * 4)
    p = look.pal
    if look.border in ("arcane", "royal"):
        for k in range(5):
            if (step + k * 3) % 5 >= 3:
                continue
            h = _hash(step // 2 * 13 + k, k)
            x = 4 + h % max(1, wu - 8)
            y = 1 if h % 2 else hu - 2
            col = look.sparks[(h // 7) % len(look.sparks)]
            for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
                g.rect(x + dx, y + dy, 1, 1, col)
    elif look.border == "prism":
        for k in range(12):
            ph = (step + k * 3) % 6
            if ph >= 4:
                continue
            h = _hash(step // 2 * 7 + k, k)
            x = 3 + h % max(1, wu - 6)
            y = 1 if h % 2 else hu - 2
            col = "#ffffff" if h % 3 else look.sparks[1 + k % 4]
            g.rect(x, y, 1, 1, col)
            if ph < 2:
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    g.rect(x + dx, y + dy, 1, 1, col)
    elif look.border == "swirl":
        # lights circling the edge, the inner ones faster: matter falling in
        per = 2 * (wu + hu - 2)
        for k in range(8):
            pos = int(t * (18 + 9 * (k % 2)) + k * per / 8) % per
            if pos < wu:
                x, y = pos, 0
            elif pos < wu + hu - 1:
                x, y = wu - 1, pos - wu + 1
            elif pos < 2 * wu + hu - 2:
                x, y = wu - 1 - (pos - wu - hu + 2), hu - 1
            else:
                x, y = 0, hu - 1 - (pos - 2 * wu - hu + 3)
            g.rect(x, y, 1, 1, look.sparks[1 + k % 2 * 2])
    elif look.border == "leaf":
        for k in range(4):
            x = int((t * 15 + k * wu / 4) % (wu + 6)) - 3
            y = (0 if k % 2 else hu - 3) + (1 if int(t * 3 + k) % 2 else 0)
            g.sprite(x, y, _LEAF3, {"X": look.sparks[k % 2], "h": look.sparks[2]})
    elif look.border == "spark":
        s8 = int(t * 9)
        if _hash(s8, 5) % 10 < 6:
            h = _hash(s8, 9)
            x = 4 + h % max(1, wu - 20)
            top = h % 2 == 0
            y = 1 if top else hu - 2
            for i in range(11):
                g.rect(x + i, y + (i % 2 if top else -(i % 2)), 1, 1,
                       "#ffffff" if i % 3 else p["accent"])
            g.rect(x + 5, y + (1 if top else -2), 1, 2, p["accent"])


def _crest(look: Look, g: Grid, wu: int, t: float) -> None:
    """What rises above the top edge, in the CREST_U rows over the pill: the
    dragon's flames (they flicker), the crown's points."""
    p = look.pal
    step = int(t * 8)
    rows = CRESTS[look.border]
    if look.border == "flame":
        # tongues of fire, each a colour of its own, licking left and right
        n = (wu - 4) // 6
        x0 = (wu - n * 6) // 2 + 1
        for j in range(n):
            x = x0 + j * 6
            hgt = 1 + _hash(j, step) % 4
            col = look.sparks[(j + step // 4) % len(look.sparks)]
            hot = mix(col, "#ffffff", 0.45)
            sway = _hash(j, step + 7) % 3 - 1
            g.rect(x, rows - 1, 5, 1, col)
            if hgt >= 2:
                g.rect(x + 1, rows - 2, 3, 1, hot if hgt == 2 else col)
            if hgt >= 3:
                g.rect(x + 1 + sway, rows - 3, 3, 1, hot)
            if hgt >= 4:
                g.rect(x + 2 + sway, rows - 4, 1, 1, mix(col, "#ffffff", 0.8))
    elif look.border == "prism":
        # glints above the pill
        for k in range(6):
            if (step + k * 2) % 5 >= 3:
                continue
            h = _hash(k * 11 + step // 3, k)
            x = 3 + h % max(1, wu - 6)
            g.rect(x, 1, 1, 1, "#ffffff")
            if (step + k) % 3 == 0:
                g.rect(x - 1, 1, 1, 1, look.sparks[1 + k % 4])
                g.rect(x + 1, 1, 1, 1, look.sparks[1 + k % 4])
                g.rect(x, 0, 1, 1, look.sparks[1 + k % 4])
                g.rect(x, 2, 1, 1, look.sparks[1 + k % 4])
    elif look.border == "wave":
        # the Great Wave: a slow rise, a curl of foam, a steep drop, rolling along
        span, off = 22, int(t * 6)
        for x in range(2, wu - 2):
            u = ((x - off) % span) / span
            h = 1 + round(3 * (u / 0.7)) if u < 0.7 else 4 - round(3 * (u - 0.7) / 0.3)
            h = max(1, min(4, h))
            top = rows - h
            for r in range(top, rows):
                g.rect(x, r, 1, 1, p["accent2"] if r == top else p["accent"] if r < rows - 1 else p["line"])
            if 0.6 <= u < 0.8 and top >= 1:
                g.rect(x, top - 1, 1, 1, p["accent2"] if u >= 0.66 else p["hi"])  # the curl of foam
                if u >= 0.7 and top >= 2:
                    g.rect(x, top - 2, 1, 1, p["accent2"])  # a claw
    elif look.border == "royal":
        # crown points: a pearl, a neck, then a widening spike
        n = max(1, (wu - 4) // 12)
        x0 = (wu - n * 12) // 2 + 6
        for j in range(n):
            cx = x0 + j * 12 - 1
            pearl = "#ffffff" if (j + step // 3) % 4 == 0 else p["accent2"]
            for r, w in enumerate((1, 3, 3, 5, 9)):
                col = pearl if r < 2 else mix(p["accent"], p["shadow"], 0.3) if r == 4 else p["accent"]
                g.rect(cx - w // 2, r, w, 1, col)
                if r >= 2:
                    g.rect(cx - w // 2, r, 1, 1, p["hi"])


# -- sprites -----------------------------------------------------------------------

_ORB = ["..ooooo..",
        ".oXXXXXo.",
        "oXhhXXXXo",
        "oXhhXXXXo",
        "oXXXXXXXo",
        "oXXXXXXXo",
        "oXXXXXXXo",
        ".oXXXXXo.",
        "..ooooo.."]
_ORB_B = ["..ooooo..",
          ".oXXXXXo.",
          "oXhXXXXXo",
          "oXXXXXXXo",
          "oXXXXXXXo",
          "oXXXXXXXo",
          "oXXXXXXXo",
          ".oXXXXXo.",
          "..ooooo.."]
_SHIELD = ["ooooooooo",
           "oXXXhXXXo",
           "oXXXhXXXo",
           "ohhhhhhho",
           "oXXXhXXXo",
           ".oXXhXXo.",
           "..oXhXo..",
           "...oXo...",
           "....o...."]
_LEAF3 = [".XX",
          "XhX",
          "X.."]
_LEAF = ["......oo.",
         "....ooXXo",
         "..ooXXXXo",
         ".oXXhXXXo",
         ".oXhXXXo.",
         "oXhXXXoo.",
         "ohXXoo...",
         "oo.o.....",
         "o........"]
_FLAME = ["....o....",
          "...oXo...",
          "...oXXo..",
          "..oXhXXo.",
          ".oXXhhXXo",
          ".oXhhhhXo",
          ".oXhhhhXo",
          "..oXhhXo.",
          "...ooooo."]
_FLAME_B = [".....o...",
            "....oXo..",
            "...oXXo..",
            "..oXhXXo.",
            ".oXXhhXXo",
            ".oXhhhhXo",
            ".oXhhhhXo",
            "..oXhhXo.",
            "...ooooo."]
_SCALES = ["....h....",
           "XXXXXXXXX",
           "X...X...X",
           "X...X...X",
           "X...X...X",
           "XXX.X.XXX",
           ".X..X..X.",
           "....X....",
           "..XXXXX.."]
_SCALES_B = ["....h....",
             "XXXXXXXXX",
             "X...X...X",
             "X...X.XXX",
             "X...X..X.",
             "X...X....",
             "XXX.X....",
             ".X..X....",
             "..XXXXX.."]
_CROWN = ["h...h...h",
          "X...X...X",
          "XX.XXX.XX",
          "XXXXXXXXX",
          "XoXXoXXoX",
          "XXXXXXXXX"]
_MOON = ["...ooo...",
         ".ooXXXo..",
         ".oXXXo...",
         "oXXXo....",
         "oXXXo....",
         "oXXXo....",
         ".oXXXo...",
         ".ooXXXo..",
         "...ooo..."]
_SUN_CORE = ["...XXX...",
             "..XXXXX..",
             "..XhXXX..",
             "..XXXXX..",
             "...XXX..."]
_PEN = ["......oo.",
        ".....oXXo",
        "....oXXo.",
        "...oXXo..",
        "..oXXo...",
        ".oXho....",
        "ohXo.....",
        "oXo......",
        "oo......."]
_PEN_B = ["......oo.",
          ".....oXXo",
          "....oXXo.",
          "...oXXo..",
          "..oXXo...",
          ".oXho....",
          "ohXo.....",
          "oXo.....o",
          "oo.....oo"]


def _hole(phase: int) -> list[str]:
    """A black hole: a dark core and spiral arms, turned a quarter each frame."""
    rows = []
    for y in range(9):
        row = ""
        for x in range(9):
            dx, dy = x - 4, y - 4
            r = math.hypot(dx, dy)
            if r > 4.3:
                row += "."
            elif r < 1.5:
                row += "k"
            else:
                arm = (math.atan2(dy, dx) / (2 * math.pi) * 2 + r * 0.5 - phase / 4) % 1
                row += ("h" if r > 3.2 else "X") if arm < 0.5 else "o"
        rows.append(row)
    return rows


_HOLES = [_hole(i) for i in range(4)]
_DRAGON = ["o.o......",
           "oXo..oo..",
           ".oXXXXXo.",
           ".XXXeXXXo",
           "XXXXXXXXX",
           "XXXXXXoXX",
           ".XXXXXXXo",
           "..oXhhXo.",
           "...ooooo."]
_GEM = ["..ooooo..",
        ".ohXhXho.",
        "ohXXhXXho",
        "ooooooooo",
        ".oXhXhXo.",
        "..oXhXo..",
        "...oXo...",
        "....o...."]
_WAVE = ["...hhhh..",
         "..hXXXXh.",
         ".hXXooXXh",
         ".XXoo..Xh",
         "XXXo....X",
         "XXXXo..h.",
         "XXXXXXXX.",
         "XXooXXXXX",
         "XXXXXXXXX"]
_STAR = ["....X....",
         "....X....",
         "...XhX...",
         "..XXhXX..",
         "XXXhhhXXX",
         "..XXhXX..",
         "...XhX...",
         "....X....",
         "....X...."]
_STAR_B = [".........",
           "....X....",
           "....h....",
           "...XhX...",
           ".XXhhhXX.",
           "...XhX...",
           "....h....",
           "....X....",
           "........."]
_BOLT = ["....XXX",
         "...XXX.",
         "..XXX..",
         ".XXXXXX",
         "..XXXX.",
         "...XXX.",
         "...XX..",
         "..XX...",
         "..X...."]
_TICK = ["....X",
         "...XX",
         "X.XX.",
         ".XX..",
         "..X.."]
_CHEV = ["X..",
         ".X.",
         "..X",
         ".X.",
         "X.."]


def _dot_pal(colour: str) -> dict:
    return {"X": colour, "h": mix(colour, "#ffffff", 0.7), "o": mix(colour, "#000000", 0.62)}


def dot(g: Grid, look: Look, cx: int, cy: int, colour: str, t: float) -> None:
    """The recording mark, centred on art pixel (cx, cy); two-step breathing."""
    b = int(t * 2.5) % 2
    pal = _dot_pal(colour)
    if look.dot == "shield":
        g.sprite(cx - 4, cy - 4, _SHIELD, pal)
        if b:
            g.rect(cx - 5, cy - 1, 1, 2, mix(colour, "#000000", 0.5))
            g.rect(cx + 5, cy - 1, 1, 2, mix(colour, "#000000", 0.5))
    elif look.dot == "star":
        g.sprite(cx - 4, cy - 4, _STAR_B if b else _STAR, pal)
        g.rect(cx - 5 + 7 * b, cy - 4 + 7 * (1 - b), 1, 1, look.sparks[1])
    elif look.dot == "bolt":
        g.sprite(cx - 3, cy - 4, _BOLT, pal)
        if b:
            g.rect(cx - 5, cy + 1, 1, 1, look.pal["accent2"])
            g.rect(cx + 4, cy - 3, 1, 1, look.pal["accent2"])
    elif look.dot == "leaf":
        g.sprite(cx - 4 + b, cy - 4, _LEAF, pal)
    elif look.dot == "flame":
        g.sprite(cx - 4, cy - 4, _FLAME_B if b else _FLAME, pal)
    elif look.dot == "scales":
        g.sprite(cx - 4, cy - 4, _SCALES_B if b else _SCALES, pal)
    elif look.dot == "crown":
        g.sprite(cx - 4, cy - 3, _CROWN, pal)
        if b:
            g.rect(cx - 4, cy - 4, 1, 1, "#ffffff")
            g.rect(cx + 4, cy - 4, 1, 1, "#ffffff")
    elif look.dot == "dragon":
        g.sprite(cx - 4, cy - 4, _DRAGON, {**pal, "e": "#ffe85a"})
        for j, c in enumerate(look.sparks[:3]):  # breath: sparks puffing from the snout
            if (b + j) % 2:
                g.rect(cx + 5 + j // 2, cy + 1 + j, 1, 1, c)
    elif look.dot == "gem":
        g.sprite(cx - 4, cy - 4, _GEM, pal)
        g.rect(cx - 5 + 9 * b, cy - 3 + b, 1, 1, "#ffffff")
        g.rect(cx + 4 - 8 * b, cy - 5, 1, 1, "#ffffff")
        if b:
            g.rect(cx - 5, cy + 1, 1, 1, "#d6e6ff")
            g.rect(cx + 5, cy, 1, 1, "#ffd6f0")
    elif look.dot == "wave":
        g.sprite(cx - 4, cy - 4, _WAVE, pal)
        g.rect(cx + 4 - b, cy - 5 + b, 1, 1, look.pal["accent2"])
        g.rect(cx + 2, cy - 5 + 2 * b, 1, 1, look.pal["accent2"])
    elif look.dot == "pen":
        g.sprite(cx - 4, cy - 4, _PEN_B if b else _PEN, pal)
    elif look.dot == "hole":
        g.sprite(cx - 4, cy - 4, _HOLES[int(t * 5) % 4], {**pal, "k": "#000000"})
    elif look.dot == "moon":
        g.sprite(cx - 4, cy - 4, _MOON, _dot_pal(mix(colour, "#000000", 0.35)))
        g.rect(cx + 2, cy - 2 + b, 1, 1, look.sparks[1])
        g.rect(cx + 1 + 2 * b, cy + 2, 1, 1, look.pal["muted"])
    elif look.dot == "sun":
        sun(g, cx, cy, _dot_pal(mix(colour, "#ffffff", 0.2)), b)
    else:
        g.sprite(cx - 4, cy - 4, _ORB_B if b else _ORB, pal)


def sun(g: Grid, cx: int, cy: int, pal: dict, b: int) -> None:
    """A disc with rays that swing between plus and cross."""
    g.sprite(cx - 4, cy - 2, _SUN_CORE, pal)
    ray = pal["X"]
    pts = ([(0, -4), (0, -3), (0, 3), (0, 4), (-4, 0), (-3, 0), (3, 0), (4, 0)] if not b else
           [(-3, -3), (3, -3), (-3, 3), (3, 3), (-4, -4), (4, -4), (-4, 4), (4, 4)])
    for dx, dy in pts:
        g.rect(cx + dx, cy + dy, 1, 1, ray)


def badge(g: Grid, look: Look, cx: int, cy: int, mode: str, colour: str, t: float, glyph: str) -> None:
    """The state mark for everything that isn't recording."""
    p = look.pal
    if mode == "busy":
        ring = [(0, -4), (3, -3), (4, 0), (3, 3), (0, 4), (-3, 3), (-4, 0), (-3, -3)]
        head = int(t * 10) % 8
        for i, (dx, dy) in enumerate(ring):
            age = (head - i) % 8
            col = colour if age == 0 else mix(colour, p["canvas"], 0.35 * age) if age < 3 else p["line"]
            g.rect(cx + dx - 1, cy + dy - 1, 2, 2, col)
        return
    pal = _dot_pal(colour)
    if mode == "command":
        pal = {"X": p["canvas"], "h": p["canvas"], "o": colour}
        g.sprite(cx - 4, cy - 4, _ORB_B if int(t * 2) % 2 else _ORB, pal)
        g.sprite(cx - 1, cy - 2, _CHEV, {"X": colour})
        return
    g.sprite(cx - 4, cy - 4, _ORB, pal)
    dark = p["shadow"]
    if mode == "done":
        g.sprite(cx - 2, cy - 2, _TICK, {"X": dark})
    elif mode == "message":
        g.rect(cx, cy - 2, 1, 3, dark)
        g.rect(cx, cy + 2, 1, 1, dark)
    elif glyph in ("✓", "ok"):
        g.sprite(cx - 2, cy - 2, _TICK, {"X": dark})
    elif glyph == "!":
        g.rect(cx, cy - 2, 1, 3, dark)
        g.rect(cx, cy + 2, 1, 1, dark)
    else:
        ch = glyph if glyph and glyph.isascii() and glyph.isprintable() else "*"
        w = text_w(ch)
        g.text(cx - w // 2, cy - 2, ch, dark)


# -- the meter ----------------------------------------------------------------------

def level_height(level: float) -> float:
    db = 20 * math.log10(max(level, 1e-5))
    return min(max((db + 60) / 40, 0.0), 1.0)


#: Art pixels from one bar to the next (swords and bolts need a gap).
PITCH = {"blade": 4, "bolt": 4, "vine": 4, "flame": 4, "stack": 5, "iris": 4, "scribble": 4, "swirl": 4, "surf": 4, "prism": 3}


def wave(g: Grid, look: Look, levels, x0: int, x1: int, base: int, max_h: int, speech: float,
         t: float) -> None:
    """Level bars in art pixels, newest on the right, growing up from ``base``
    (the row just under the lowest bar pixel)."""
    p = look.pal
    pitch = PITCH.get(look.wave, 3)
    n = max(1, (x1 - x0) // pitch)
    levels = list(levels)[-n:]
    levels = [0.0] * (n - len(levels)) + levels
    x0 += (x1 - x0 - n * pitch + 1) // 2
    st = int(t * 9)
    for i, level in enumerate(levels):
        x = x0 + i * pitch
        lh = level_height(level)
        loud = level >= speech
        h = max(1, round(lh * max_h)) if loud else 1
        if look.wave == "led":
            if not loud:
                g.rect(x, base - 1, 2, 1, p["line"])
                continue
            blocks = max(1, round(h / 3))
            for b in range(blocks):
                top = b == blocks - 1
                g.rect(x, base - (b * 3 + 2), 2, 2,
                       p["text"] if top and blocks > 2 else p["accent"] if b < 2 else p["hi"])
        elif look.wave == "blade":
            # a sword: point, blade, crossguard, red grip, pommel; all alike
            if not loud:
                g.rect(x, base - 1, 3, 1, p["line"])
                continue
            hh = max(4, h)
            g.rect(x + 1, base - hh, 1, hh - 3, p["text"])
            g.rect(x, base - 3, 3, 1, p["accent"])
            g.rect(x + 1, base - 2, 1, 1, p["accent2"])
            g.rect(x + 1, base - 1, 1, 1, p["accent"])
        elif look.wave == "rune":
            if not loud:
                g.rect(x + 1, base - 1, 1, 1, p["line"])
                continue
            for y in range(1, h + 1, 2):
                top = y + 2 > h
                g.rect(x + 1 if not top else x, base - y, 1 if not top else 3, 1,
                       look.sparks[0] if top else p["accent"] if (y // 2 + i) % 2 else p["hi"])
            if h > 5 and (i + int(t * 4)) % 3 == 0:
                g.rect(x + 1, base - h - 2, 1, 1, look.sparks[2])
        elif look.wave == "bolt":
            # lightning: a one-pixel line that kinks left and right, redrawn each flicker
            if not loud:
                g.rect(x, base - 1, 3, 1, p["line"])
                continue
            pos, step, run = 1, 1 if _hash(i, st) % 2 else -1, 0
            for y in range(max(2, h)):
                if run <= 0 or not 0 <= pos + step <= 2:
                    step = -step if run <= 0 else step
                    run = 2 + _hash(i * 7 + y, st) % 2
                    if not 0 <= pos + step <= 2:
                        step = -step
                pos += step
                run -= 1
                g.rect(x + pos, base - 1 - y, 1, 1, "#ffffff" if y >= h - 2 else p["accent"])
        elif look.wave == "vine":
            if not loud:
                g.rect(x + 1, base - 1, 1, 1, p["line"])
                continue
            g.rect(x + 1, base - h, 1, h, p["line"])
            for y in range(1, h, 2):
                g.rect(x + 2 * (((y // 2) + i) % 2), base - 1 - y, 1, 1, p["accent"])
            g.rect(x + 1, base - h - 1, 1, 1, p["accent2"])
        elif look.wave == "flame":
            # a tongue of fire per bar, each its own colour of the flame
            col = look.sparks[i % len(look.sparks)]
            if not loud:
                g.rect(x + 1, base - 1, 1, 1, col)
                continue
            for y in range(h):
                row = base - 1 - y
                if y < max(2, h * 0.55):
                    g.rect(x, row, 3, 1, col)
                    if y > 0:
                        g.rect(x + 1, row, 1, 1, mix(col, "#ffffff", 0.7))
                else:
                    off = 1 + (_hash(i, st) % 3 - 1 if y == h - 1 and h > 3 else 0)
                    g.rect(x + off, row, 1, 1, mix(col, "#ffffff", 0.8 if y == h - 1 else 0.3))
        elif look.wave == "stack":
            # a pile of books and papers: each layer two rows, nudged left or right
            if not loud:
                g.rect(x, base - 1, 3, 1, p["line"])
                continue
            tints = (p["accent"], p["text"], p["hi"], mix(p["accent"], p["shadow"], 0.45))
            for b in range(max(1, round(h / 2))):
                col = tints[(i + b) % 4]
                nudge = _hash(i * 5 + b, 3) % 2
                g.rect(x + nudge, base - 2 - b * 2, 3, 1, col)
                g.rect(x + nudge, base - 1 - b * 2, 3, 1, mix(col, p["shadow"], 0.45))
        elif look.wave == "iris":
            # iridescent: purple at the foot to light blue at the tip, shimmering
            if not loud:
                g.rect(x, base - 1, 2, 1, p["line"])
                continue
            tones = ("#7a52ff", "#8f6cff", "#6f8cff", "#52a8ff", "#6ad0ff")
            for y in range(h):
                c = tones[min(4, y * 5 // max(1, h) + ((st + i) // 3) % 2)]
                g.rect(x, base - 1 - y, 2, 1, c)
                g.rect(x + 2, base - 1 - y, 1, 1, mix(c, "#2a2540", 0.45))
            g.rect(x, base - h - 1, 2, 1, "#b9a6ff")
        elif look.wave == "scribble":
            # a pen scribbling up: zig-zag strokes, a dot of ink at the tip
            if not loud:
                g.rect(x, base - 1, 3, 1, p["line"])
                continue
            prev = None
            for y in range(max(2, h)):
                cur = (0, 2)[(y // 2 + i + st // 2) % 2]
                lo, hi = (cur, cur) if prev is None else (min(prev, cur), max(prev, cur))
                g.rect(x + lo, base - 1 - y, hi - lo + 1, 1, p["accent"])
                prev = cur
            g.rect(x + 1, base - h - 1, 1, 1, p["accent"])
        elif look.wave == "swirl":
            # a whirlpool: rows that wind round a line, purple to white at the tip
            if not loud:
                g.rect(x + 1, base - 1, 2, 1, p["line"])
                continue
            tints = (p["accent"], p["hi"], p["accent2"], p["hi"], p["accent"])
            for y in range(h):
                off = round(math.sin(y * 0.9 + i * 0.8 + t * 6) * 1.0) + 1
                g.rect(x + off, base - 1 - y, 2, 1,
                       "#ffffff" if y == h - 1 else tints[(y // 2 + st + i) % 5])
        elif look.wave == "prism":
            # light prismatic bars, white glints all round them
            if not loud:
                g.rect(x, base - 1, 2, 1, p["line"])
                continue
            tints = look.sparks[1:]
            for y in range(h):
                g.rect(x, base - 1 - y, 2, 1, tints[(y // 2 + i + st // 3) % len(tints)])
            g.rect(x, base - h - 1, 2, 1, "#ffffff")
            for j in range(2):
                if _hash(i * 3 + j, st) % 3 == 0:
                    g.rect(x - 1 + _hash(i + j, st + 5) % 4, base - 1 - _hash(i, st + 9 + j) % (h + 2),
                           1, 1, "#ffffff")
        elif look.wave == "surf":
            # swells with foam on top, a flick of spray
            if not loud:
                g.rect(x, base - 1, 3, 1, p["line"])
                continue
            for y in range(h):
                g.rect(x, base - 1 - y, 3, 1,
                       p["accentdark"] if y < h * 0.3 else p["accent"] if y < h - 2 else p["hi"])
            g.rect(x, base - h, 3, 1, p["accent2"])
            g.rect(x + 1 + (i + st // 2) % 2, base - h - 1, 1, 1, p["accent2"])
        elif look.wave == "gems":
            if not loud:
                g.rect(x + 1, base - 1, 1, 1, p["line"])
                continue
            g.rect(x, base - h, 1, h, p["hi"])
            g.rect(x + 1, base - h, 1, h, p["accent"])
            g.rect(x + 2, base - h, 1, h, p["accentdark"])
            g.rect(x + 1, base - h - 1, 1, 1, "#ffffff" if (i + int(t * 3)) % 5 == 0 else p["accent2"])
        else:  # bar
            if not loud:
                g.rect(x, base - 1, 2, 1, p["line"])
                continue
            g.rect(x, base - h, 1, h, p["accent"])
            g.rect(x + 1, base - h, 1, h, mix(p["accent"], p["canvas"], 0.4))
            g.rect(x, base - h, 2, 1, p["text"])


# -- whole frames --------------------------------------------------------------------

def _wrap(tokens: list[tuple[str, str]], width: int) -> list[list[tuple[int, str, str]]]:
    lines: list[list[tuple[int, str, str]]] = [[]]
    x = 0
    for word, colour in tokens:
        w = text_w(word)
        if x and x + 4 + w > width:
            lines.append([])
            x = 0
        if x:
            x += 4
        lines[-1].append((x, word, colour))
        x += w
    return lines


def live_lines(look: Look, context: list[str], current: list[str], cut: bool, width: int):
    """Live words wrapped into at most two lines: the dimmed context, then the
    current sentence; the oldest words drop behind '..' when too long."""
    p = look.pal
    dim = mix(p["text"], p["canvas"], 0.5)
    tokens = [(w.upper(), dim) for w in context] + [(w.upper(), p["text"]) for w in current]
    if cut:
        tokens.insert(0, ("..", dim))
    lines = _wrap(tokens, width)
    while len(lines) > 2 and len(tokens) > 1:
        body = tokens[1:] if tokens[0][0] == ".." else tokens
        tokens = [("..", dim)] + body[1:]
        lines = _wrap(tokens, width)
    return lines


def fit(label: str, avail: int) -> str:
    label = label.upper()
    if text_w(label) <= avail:
        return label
    while label and text_w(label + ".") > avail:
        label = label[:-1]
    return label.rstrip() + "."


def draw(c, theme, x0: float, y1: float, w_px: float, s: float, now: float, mode: str,
         levels=(), dot_kind: str = "llm", elapsed: float = 0.0, label: str = "",
         glyph: str = "!", colour: str = "", live=None, speech: float = 0.009) -> None:
    """One whole pill, bottom edge at y1, in ``w_px`` screen pixels (centred on
    the art grid). ``mode`` is recording / busy / done / command / notice /
    message. ``live`` is (context words, current words, cut) or None."""
    look = LOOKS[theme.pixel]
    p = look.pal
    k = cell(s)
    wu = max(40, int(w_px // k))
    lines = live_lines(look, *live, wu - 10) if live and live[1] + live[0] else []
    extra = len(lines) * LINE_U + TEXT_PAD - TEXT_TAIL if lines else 0
    hu = ROW_U + extra
    ox = x0 + (w_px - wu * k) // 2
    oy = y1 - hu * k
    step = int(now * 4)
    g = Grid(c, ox, oy, k)
    ph = _photo(c, ("body", look.key, wu, hu, step, k),
                lambda: _scaled(_body(look, wu, hu, step), k))
    c.create_image(g.ox, g.oy, image=ph, anchor="nw")
    if look.border in CRESTS:
        _crest(look, Grid(c, ox, oy - (CRESTS[look.border] - 1) * k, k), wu, now)  # its last row sits on the border
    _extras(look, g, wu, hu, now)

    for j, line in enumerate(lines):
        for x, word, col in line:
            g.text(5 + x, TEXT_PAD + j * LINE_U, word, col)
    mid = hu - (ROW_U + 1) // 2
    up = (1 + (look.dot in ("shield", "star", "bolt"))) if lines else 0
    if mode == "recording":
        dot(g, look, 9, mid - up, DOTS.get(dot_kind, DOTS["llm"]), now)
        secs = int(elapsed)
        stamp = f"{secs // 60}:{secs % 60:02d}"
        g.text(wu - 5, mid - 2, stamp, p["text"], anchor="e")
        x_end = wu - 5 - text_w(stamp) - 4
        wave(g, look, levels, 17, x_end, mid + 5, 7, speech, now)
    else:
        badge(g, look, 9, mid - up, mode, colour or p["accent"], now, glyph)
        g.text(17, mid - 2, fit(label, wu - 17 - 6), p["text"])


def label_box(c, theme, x0: float, y0: float, x1: float, y1: float, s: float, text: str = "") -> None:
    """A click label / chip / grid number: a square beveled box in the look,
    its number in Silkscreen (scaled up for big boxes)."""
    look = LOOKS[theme.pixel]
    p = look.pal
    k = cell(s)
    # Never smaller than the number needs: a 1-pixel edge, a bevel, a pixel of air.
    need_w = text_w(text.upper()) + 7 if text else 5
    wu, hu = max(need_w, int((x1 - x0) // k)), max(CAP_U + 6, int((y1 - y0) // k))
    g = Grid(c, x0 + ((x1 - x0) - wu * k) // 2, y0 + ((y1 - y0) - hu * k) // 2, k)
    edge = p["accent"]
    g.rect(0, 0, wu, hu, p["shadow"])
    g.rect(1, 1, wu - 2, hu - 2, edge)
    g.rect(2, 2, wu - 4, hu - 4, p["canvas"])
    g.rect(2, 2, wu - 4, 1, mix(p["canvas"], p["hi"], 0.25))
    if text:
        text = text.upper()
        w = max(1, text_w(text))
        scale = max(1, min((wu - 6) // w, (hu - 6) // CAP_U, 12))
        g.text(wu // 2, (hu - CAP_U * scale) // 2, text, p["text"], scale=scale, anchor="c")


def burst(c, theme, cx: float, cy: float, s: float, prog: float) -> None:
    """Where pasted text landed: plus-shaped pixel sparks flying out."""
    look = LOOKS[theme.pixel]
    k = cell(s)
    ease = 1 - (1 - prog) ** 3
    g = Grid(c, cx, cy, k)
    n = 14
    for i in range(n):
        a = i * 2 * math.pi / n + (0.2 if i % 2 else 0)
        r = (3 + (7 + (i % 3) * 3) * ease) * 2.2
        x, y = round(math.cos(a) * r), round(math.sin(a) * r)
        col = look.sparks[i % len(look.sparks)]
        if prog > 0.6 and i % 2:
            continue
        g.rect(x, y, 1, 1, col)
        if prog < 0.45:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                g.rect(x + dx, y + dy, 1, 1, mix(col, look.pal["canvas"], 0.4))
    if prog < 0.25:
        g.rect(-1, -1, 3, 3, look.sparks[0])


def preview(c, theme, x: float, y: float, w: float, now: float, mode: str = "recording") -> None:
    """The pill as Settings shows it: fake speech, ``mode`` is recording or done."""
    levels = []
    for i in range(24):
        v = now * 6 - (24 - i) * 0.45
        env = 0.5 + 0.5 * math.sin(v * 0.9) * math.sin(v * 0.37 + 1)
        levels.append(10 ** (-60 / 20 + 40 / 20 * max(0.0, env)) * (1 if env > 0.15 else 0.2))
    if mode == "recording":
        draw(c, theme, x, y + ROW_U * cell(1.0), PILL_U * cell(1.0), 1.0, now, "recording",
             levels, elapsed=now % 60)
    else:
        draw(c, theme, x, y + ROW_U * cell(1.0), PILL_U * cell(1.0), 1.0, now, "done",
             label="INSERTED")


def panel(c, theme, x0: float, y0: float, x1: float, y1: float, s: float, now: float = 0.0) -> None:
    """Just the body and border, filling the box (the command sheet, lists)."""
    look = LOOKS[theme.pixel]
    k = cell(s)
    wu, hu = max(12, int((x1 - x0) // k)), max(8, int((y1 - y0) // k))
    step = int(now * 4)
    ph = _photo(c, ("body", look.key, wu, hu, step, k), lambda: _scaled(_body(look, wu, hu, step), k))
    c.create_image(int(x0), int(y0), image=ph, anchor="nw")


def dot_at(c, theme, cx: float, cy: float, s: float, colour: str, t: float) -> None:
    k = cell(s)
    dot(Grid(c, cx, cy, k), LOOKS[theme.pixel], 0, 0, colour, t)


def wave_at(c, theme, levels, x0: float, x1: float, mid: float, max_h: float, s: float,
            speech: float, t: float) -> None:
    k = cell(s)
    g = Grid(c, x0, mid, k)
    wave(g, LOOKS[theme.pixel], levels, 0, int((x1 - x0) // k), int(max_h // k // 2), int(max_h // k),
         speech, t)
