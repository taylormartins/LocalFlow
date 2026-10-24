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
ROW_U = 13
PILL_U = 76
#: Caps are 5 art pixels tall; a line of live text is 5 + 3 apart.
LINE_U = 7
CAP_U = 5
#: Two lines of live text above the pill row, with padding.
TEXT_U = 2 * LINE_U + 3

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
    border: str      # block | stone | arcane | spark
    dot: str         # orb | shield | star | bolt
    wave: str        # led | blade | rune | bolt
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
        self.c.create_rectangle(self.ox + x * k, self.oy + y * k, self.ox + (x + w) * k - 1,
                                self.oy + (y + h) * k - 1, fill=colour, outline="")

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
    the knight merlons along the top."""
    m = np.ones((hu, wu), bool)
    if battlements and hu > 8:
        for x in range(wu):
            if (x // 4) % 2 == 1:
                m[:2, x] = False
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
    if look.border == "arcane":
        for k in range(5):
            if (step + k * 3) % 5 >= 3:
                continue
            h = _hash(step // 2 * 13 + k, k)
            x = 4 + h % max(1, wu - 8)
            y = 1 if h % 2 else hu - 2
            col = look.sparks[(h // 7) % len(look.sparks)]
            for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
                g.rect(x + dx, y + dy, 1, 1, col)
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
_SHIELD = [".ooooooo.",
           "oXXXhXXXo",
           "oXXXhXXXo",
           "ohhhhhhho",
           "oXXXhXXXo",
           ".oXXhXXo.",
           "..oXhXo..",
           "...ooo..."]
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
    else:
        g.sprite(cx - 4, cy - 4, _ORB_B if b else _ORB, pal)


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


def wave(g: Grid, look: Look, levels, x0: int, x1: int, base: int, max_h: int, speech: float,
         t: float) -> None:
    """Level bars in art pixels, newest on the right, growing up from ``base``
    (the row just under the lowest bar pixel)."""
    p = look.pal
    n = max(1, (x1 - x0) // 3)
    levels = list(levels)[-n:]
    levels = [0.0] * (n - len(levels)) + levels
    x0 += (x1 - x0 - n * 3 + 1) // 2
    for i, level in enumerate(levels):
        x = x0 + i * 3
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
            if not loud:
                g.rect(x, base - 1, 2, 1, p["line"])
                continue
            g.rect(x, base - h, 1, h, p["text"])
            g.rect(x + 1, base - h + 1, 1, h - 1, p["hi"])
            g.rect(x - 1 if i % 2 else x, base - 2, 3 if i % 2 else 2, 1, p["accent"])
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
        else:  # bolt: a zig-zag, white in the core
            if not loud:
                g.rect(x, base - 1, 2, 1, p["line"])
                continue
            for y in range(h):
                off = 1 if (y // 3) % 2 else 0
                g.rect(x + off, base - 1 - y, 2, 1, "#ffffff" if y == h - 1 else p["accent"])


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
    extra = len(lines) * LINE_U + 3 if lines else 0
    hu = ROW_U + extra
    ox = x0 + (w_px - wu * k) // 2
    oy = y1 - hu * k
    step = int(now * 4)
    g = Grid(c, ox, oy, k)
    ph = _photo(c, ("body", look.key, wu, hu, step, k),
                lambda: _scaled(_body(look, wu, hu, step), k))
    c.create_image(g.ox, g.oy, image=ph, anchor="nw")
    _extras(look, g, wu, hu, now)

    for j, line in enumerate(lines):
        for x, word, col in line:
            g.text(5 + x, 3 + j * LINE_U, word, col)
    mid = hu - (ROW_U + 1) // 2
    if mode == "recording":
        dot(g, look, 9, mid, DOTS.get(dot_kind, DOTS["llm"]), now)
        secs = int(elapsed)
        stamp = f"{secs // 60}:{secs % 60:02d}"
        g.text(wu - 5, mid - 2, stamp, p["text"], anchor="e")
        x_end = wu - 5 - text_w(stamp) - 4
        wave(g, look, levels, 17, x_end, mid + 5, 9, speech, now)
    else:
        badge(g, look, 9, mid, mode, colour or p["accent"], now, glyph)
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
