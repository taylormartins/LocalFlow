"""Pill themes: colours, font, border, waveform and recording-dot style.

Built-in themes:
- Royal (the default): black and gold, a framed border with diamond
  ornaments, a straight-line waveform, stardust for the recording mark.
- Gemstone: light through crystal: a brilliant-cut gem (ruby, sapphire,
  diamond), rainbow fire, an iridescent border, a prism waveform.
- Smooth: white and near-black, soft capsule waveform.
- Silver: a MacBook or iPhone: light grey with a white border, grey
  lettering, a waveform of fine grey hairlines.
- Classic: the original pill.
- Matrix: black and green, code lettering, columns of glyphs.
- Doodle: pencil on paper, a sketched border and a scribbled line.
- Newspaper: black and white newsprint, printed rules, halftone dots.
- Modern: a dot-matrix meter.
- Fire: light grey, a border of flickering flames, a waveform of flames
  and a flame for the recording mark.
- Ice: black with white lettering, a border of frost and ice crystals, an
  icy wind for the waveform and a turning snowflake.

The numbered click labels, the window list and the grid can wear a theme
too (overlay.label_theme): the pill's, any other, or the classic yellow.

Your own are made in Settings > Theme builder and kept in themes.json next
to config.toml (this PC only). The pill and the Settings preview draw through
the same functions here, so the preview is exactly what the pill looks like.
"""

from __future__ import annotations

import colorsys
import json
import logging
import math
import random
import time
from dataclasses import asdict, dataclass, fields, replace

from . import pixel
from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

STORE = PROJECT_ROOT / "themes.json"

WAVE_STYLES = ("bars", "capsules", "hairlines", "line", "zigzag", "prism", "dots", "dotgrid",
               "glyphs", "scribble", "halftone", "flames", "wind", "led", "blade", "rune", "bolt")
BORDERS = ("plain", "bevel", "ornate", "prism", "sketch", "rule", "flame", "frost", "pixel")
DOT_STYLES = ("circle", "stardust", "gem", "cursor", "sketch", "square", "flame", "snowflake",
              "orb", "shield", "star", "bolt")


@dataclass
class Theme:
    name: str = "Classic"
    #: Pill background, text, and an outline round the pill ("" = none).
    bg: str = "#1d1e22"
    text: str = "#ececee"
    edge: str = ""
    #: The border (BORDERS): plain, ornate (a double frame with diamonds), flame...
    border: str = "plain"
    #: Corner rounding: 1 = a full pill, 0 = square corners.
    round: float = 1.0
    #: The waveform: loud parts, quiet parts, and its style (WAVE_STYLES).
    wave: str = "#ececee"
    dim: str = "#4b4d55"
    wave_style: str = "bars"
    #: The recording dot (DOT_STYLES). Its colour still tells LLM / rules / raw.
    dot: str = "circle"
    font: str = "Segoe UI"
    font_size: int = 13
    #: Replaces the state colours (working, done, command mode) when set.
    accent: str = ""
    #: Black and white only: errors take the accent too (they keep their "!").
    mono: bool = False
    #: A pixel look (pixel.LOOKS) drawn by pixel.py instead of the shapes here.
    pixel: str = ""


ROYAL = Theme(
    name="Royal", bg="#0a0907", text="#f3dc95", edge="#c9a227", border="ornate", round=1.0,
    wave="#e6c158", dim="#5e4b1a", wave_style="zigzag", dot="stardust",
    font="Palatino Linotype", font_size=13, accent="#d4af37",
)
GEMSTONE = Theme(
    name="Gemstone", bg="#0b0a14", text="#eef0ff", edge="#9aa6ff", border="prism", round=1.0,
    wave="#e8ecff", dim="#262a44", wave_style="prism", dot="gem",
    font="Segoe UI Variable Display", font_size=13, accent="#b9c4ff",
)
SMOOTH = Theme(
    name="Smooth", bg="#f5f5f7", text="#1d1d1f", edge="#d2d2d7", round=1.0, wave="#48484a",
    dim="#c7c7cc", wave_style="capsules", dot="circle", font="Segoe UI Variable Text",
    font_size=13, accent="#0a84ff",
)
CLASSIC = Theme()
MATRIX = Theme(
    name="Matrix", bg="#000000", text="#33ff66", edge="#0f7a2a", round=0.0, wave="#39ff6a",
    dim="#0b3d17", wave_style="glyphs", dot="cursor", font="Consolas", font_size=13,
    accent="#33ff66",
)
DOODLE = Theme(
    name="Doodle", bg="#fbf8ef", text="#2f2f33", edge="#3b3b40", border="sketch", round=0.7,
    wave="#3b3b40", dim="#b9b4a6", wave_style="scribble", dot="sketch", font="Ink Free",
    font_size=14, accent="#3b3b40", mono=True,
)
NEWSPAPER = Theme(
    name="Newspaper", bg="#f2efe6", text="#111111", edge="#111111", border="rule", round=0.18,
    wave="#111111", dim="#b7b2a5", wave_style="halftone", dot="square", font="Georgia",
    font_size=13, accent="#111111", mono=True,
)
MODERN = Theme(
    name="Modern", bg="#0f1317", text="#e8eef4", edge="#26313b", round=1.0, wave="#7dd3fc",
    dim="#24313c", wave_style="dotgrid", dot="circle", font="Segoe UI Variable Display",
    font_size=13, accent="#7dd3fc",
)
SILVER = Theme(
    name="Silver", bg="#e6e6eb", text="#3a3a3c", edge="#ffffff", border="bevel", round=1.0,
    wave="#636366", dim="#c2c2c7", wave_style="hairlines", dot="circle",
    font="Segoe UI Variable Text", font_size=13, accent="#8e8e93",
)
FIRE = Theme(
    name="Fire", bg="#e7e5e2", text="#3b1d10", edge="#ff6a13", border="flame", round=1.0,
    wave="#ff6a13", dim="#cfc2b6", wave_style="flames", dot="flame", font="Bahnschrift",
    font_size=13, accent="#e2510c",
)
ICE = Theme(
    name="Ice", bg="#030507", text="#ffffff", edge="#bfeaff", border="frost", round=1.0,
    wave="#e6f8ff", dim="#1b2b36", wave_style="wind", dot="snowflake",
    font="Segoe UI Variable Display", font_size=13, accent="#a6e4ff",
)


def _pixel_theme(key: str) -> Theme:
    look = pixel.LOOKS[key]
    p = look.pal
    return Theme(name=look.title.title(), bg=p["canvas"], text=p["text"], edge=p["line"],
                 border="pixel", round=0.0, wave=p["accent"], dim=p["line"], wave_style=look.wave,
                 dot=look.dot, font=pixel.FAMILY, font_size=16, accent=p["accent"], pixel=key)


#: The new look (overlay.pixel): four pixel themes, shown first in Settings.
PIXEL = {f"pixel-{k}": _pixel_theme(k) for k in pixel.LOOKS}
#: The theme LocalFlow starts with when the pixel themes are ticked off
#: (overlay.theme = "royal"): the legacy v2 look.
DEFAULT = ROYAL
BUILT_IN = {"royal": ROYAL, "gemstone": GEMSTONE, "silver": SILVER, "smooth": SMOOTH,
            "modern": MODERN, "fire": FIRE, "ice": ICE,
            "classic": CLASSIC, "matrix": MATRIX, "doodle": DOODLE, "newspaper": NEWSPAPER,
            **PIXEL}


#: Smallest text on the labels and the command sheet: 13 px = 10 pt at 100%
#: (12 px in a script font like Royal's was too small to read).
LABEL_MIN = 13
#: The click labels' original look: yellow on near-black, readable over anything.
CLASSIC_LABELS = Theme(name="Classic yellow", bg="#1d1e22", text="#ffd60a", edge="#ffd60a",
                       round=0.0, wave="#ffd60a", accent="#ffd60a", font="Segoe UI")
#: overlay.label_theme: "match" follows the pill, "yellow" is the original.
LABEL_CHOICES = {"match": "Match the pill", "yellow": "Classic yellow"}


def _from_dict(name: str, d: dict) -> Theme:
    known = {f.name for f in fields(Theme)}
    t = Theme(**{k: v for k, v in d.items() if k in known})
    t.name = name
    if t.wave_style not in WAVE_STYLES:
        t.wave_style = "bars"
    if t.border not in BORDERS:
        t.border = "plain"
    if t.pixel not in pixel.LOOKS:
        t.pixel = ""
    if t.dot not in DOT_STYLES:
        t.dot = "circle"
    try:
        t.font_size = max(8, min(int(t.font_size), 24))
        t.round = max(0.0, min(float(t.round), 1.0))
    except (TypeError, ValueError):
        t.font_size, t.round = 13, 1.0
    return t


def load_custom() -> dict[str, Theme]:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("could not read %s: %s", STORE.name, exc)
        return {}
    return {k.lower(): _from_dict(k, v) for k, v in data.items() if isinstance(v, dict)}


def save_custom(theme: Theme) -> None:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    d = asdict(theme)
    d.pop("name")
    data[theme.name] = d
    STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def delete_custom(name: str) -> None:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return
    data = {k: v for k, v in data.items() if k.lower() != name.lower()}
    STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def all_themes() -> dict[str, Theme]:
    """Built-in first, then yours (a custom theme can't replace a built-in)."""
    out = dict(BUILT_IN)
    for k, t in load_custom().items():
        out.setdefault(k, t)
    return out


def legacy_themes() -> dict[str, Theme]:
    """The pre-pixel themes (legacy v2) and your own: what the ticker returns to."""
    return {k: t for k, t in all_themes().items() if k not in PIXEL}


def effective(ov) -> str:
    """The theme name in use for an OverlayConfig: a pixel theme while
    overlay.pixel is on, else the legacy overlay.theme."""
    if getattr(ov, "pixel", False):
        key = f"pixel-{(getattr(ov, 'pixel_theme', '') or 'block').lower()}"
        return key if key in PIXEL else "pixel-block"
    return ov.theme


def resolve(name: str) -> Theme:
    """A theme by name; "default" (or anything unknown) is the default."""
    return all_themes().get((name or "").lower(), DEFAULT)


def label_theme(choice: str, pill_theme: Theme) -> Theme:
    """The look of the click labels for overlay.label_theme."""
    choice = (choice or "match").lower()
    if choice == "match":
        return pill_theme
    if choice == "yellow":
        return CLASSIC_LABELS
    return resolve(choice)


class Current:
    """The theme by name, re-read when themes.json changes (at most once a
    second: the pill asks every frame)."""

    def __init__(self):
        self._key = None
        self._theme = DEFAULT
        self._checked = 0.0

    def get(self, name: str) -> Theme:
        now = time.monotonic()
        if now - self._checked > 1.0 or self._key is None or self._key[0] != name:
            self._checked = now
            try:
                mtime = STORE.stat().st_mtime
            except OSError:
                mtime = 0.0
            key = (name, mtime)
            if key != self._key:
                self._key, self._theme = key, resolve(name)
        return self._theme


# -- colours ---------------------------------------------------------------------

def blend(a: str, b: str, amount: float) -> str:
    """``a`` moved ``amount`` of the way towards ``b`` (#rrggbb colours)."""
    try:
        ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
        cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return a
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(ca, cb))


def colour_for(theme: Theme, state_colour: str, error_colour: str) -> str:
    """A state's colour under this theme: the accent, except errors (unless
    the theme is black and white only)."""
    if theme.accent and (state_colour != error_colour or theme.mono):
        return theme.accent
    return state_colour


#: The recording dot: LLM cleanup, algorithmic cleanup, raw (no cleanup).
DOTS = {"llm": "#43b974", "rules": "#4c9aff"}
#: ...as gems: ruby, sapphire, diamond.
GEMS = {"llm": "#e0115f", "rules": "#2f6bff", "raw": "#eaf6ff"}
#: ...as stardust: gold, starlight blue, white.
STARDUST = {"llm": "#f6cf5a", "rules": "#a9c8ff", "raw": "#ffffff"}
#: ...as a flame: orange, a blue gas flame, yellow.
FLAMES = {"llm": "#ff5a1f", "rules": "#2f7bff", "raw": "#f2b705"}
#: ...as a snowflake: ice, violet ice, white.
FROST = {"llm": "#8fe9ff", "rules": "#9d8cff", "raw": "#ffffff"}


def hue(h: float, sat: float = 0.55, val: float = 1.0) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, sat, val)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def dot_colour(theme: Theme, kind: str) -> str:
    """The recording dot. Its three meanings stay apart in every theme."""
    if theme.pixel:
        return pixel.DOTS[kind]
    if theme.dot == "gem":
        return GEMS[kind]
    if theme.dot == "stardust":
        return STARDUST[kind]
    if theme.dot == "flame":
        return FLAMES[kind]
    if theme.dot == "snowflake":
        return FROST[kind]
    if kind == "raw":
        return theme.text
    if theme.mono:
        # Black and white: LLM solid, rules half-tone; raw is the text colour.
        return theme.accent if kind == "llm" else blend(theme.accent, theme.bg, 0.55)
    if theme.accent:
        return theme.accent if kind == "llm" else blend(theme.accent, theme.bg, 0.5)
    return DOTS[kind]


def font(theme: Theme, s: float, bold: bool = False, size: int | None = None) -> tuple:
    if theme.pixel:  # Silkscreen only looks right in whole multiples of 8 px
        return (pixel.tk_family(), -pixel.pixel_px((size or theme.font_size) * s))
    px = -int((size or theme.font_size) * s)
    if bold and theme.font == "Segoe UI":
        return ("Segoe UI Semibold", px)
    return (theme.font, px, "bold") if bold else (theme.font, px)


# -- shapes ----------------------------------------------------------------------

def rounded(c, x0: float, y0: float, x1: float, y1: float, r: float, fill: str) -> None:
    r = max(0.0, min(r, (y1 - y0) / 2, (x1 - x0) / 2))
    c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill)
    c.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, outline=fill)
    if r > 0:
        for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r), (x0 + r, y1 - r), (x1 - r, y1 - r)):
            c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=fill)


def _outline_points(x0, y0, x1, y1, r, step=6.0):
    """Points round a rounded rectangle, clockwise from the top left."""
    r = max(0.0, min(r, (y1 - y0) / 2, (x1 - x0) / 2))
    pts = []

    def arc(cx, cy, a0):
        for i in range(7):
            a = math.radians(a0 + i * 15)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))

    def line(ax, ay, bx, by):
        n = max(1, int(math.hypot(bx - ax, by - ay) / step))
        for i in range(n):
            pts.append((ax + (bx - ax) * i / n, ay + (by - ay) * i / n))

    line(x0 + r, y0, x1 - r, y0)
    arc(x1 - r, y0 + r, 270)
    line(x1, y0 + r, x1, y1 - r)
    arc(x1 - r, y1 - r, 0)
    line(x1 - r, y1, x0 + r, y1)
    arc(x0 + r, y1 - r, 90)
    line(x0, y1 - r, x0, y0 + r)
    arc(x0 + r, y0 + r, 180)
    return pts


def _diamond(c, cx, cy, size, fill, outline):
    c.create_polygon(cx, cy - size, cx + size * 0.75, cy, cx, cy + size, cx - size * 0.75, cy,
                     fill=fill, outline=outline)


def pill(c, theme: Theme, x0: float, y0: float, x1: float, y1: float, r: float, s: float,
         t: float = 0.0) -> None:
    """The pill's body and border. Everything stays inside x0..x1, y0..y1."""
    if theme.pixel:
        pixel.panel(c, theme, x0, y0, x1, y1, s, t)
        return
    r *= theme.round
    edge = theme.edge
    if not edge:
        rounded(c, x0, y0, x1, y1, r, theme.bg)
        return
    e = max(1.0, 1.4 * s)
    if theme.border == "ornate":
        # Room above and below for the diamonds, so none is cut off.
        y0, y1 = y0 + 3.4 * s, y1 - 3.4 * s
    if theme.border == "flame":
        _flame_border(c, theme, x0, y0, x1, y1, r, s, t)
        return
    if theme.border == "frost":
        _frost_border(c, theme, x0, y0, x1, y1, r, s, t)
        return
    if theme.border == "sketch":
        # Paper, then two loose pencil passes round it, re-drawn now and then.
        rounded(c, x0 + 2 * s, y0 + 2 * s, x1 - 2 * s, y1 - 2 * s, r, theme.bg)
        rng = random.Random(int(t * 4))
        for k in range(2):
            pts = _outline_points(x0 + 2.5 * s, y0 + 2.5 * s, x1 - 2.5 * s, y1 - 2.5 * s,
                                  max(0.0, r - 2 * s), 9 * s)
            flat = []
            for px, py in pts:
                flat += [px + rng.uniform(-1.1, 1.1) * s, py + rng.uniform(-1.1, 1.1) * s]
            c.create_line(*flat, flat[0], flat[1], fill=edge if k == 0 else blend(edge, theme.bg, 0.4),
                          width=max(1.0, (1.3 - 0.4 * k) * s), smooth=True)
        return
    if theme.border == "prism":
        # Light through crystal: the edge runs through the rainbow, slowly turning.
        rounded(c, x0, y0, x1, y1, r, theme.bg)
        e2 = max(1.2, 1.6 * s)
        pts = _outline_points(x0 + e2 / 2, y0 + e2 / 2, x1 - e2 / 2, y1 - e2 / 2,
                              max(0.0, r - e2 / 2), 4 * s)
        n = len(pts)
        for i in range(n):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % n]
            c.create_line(ax, ay, bx, by, fill=hue(i / n + t * 0.08, 0.45, 1.0), width=e2,
                          capstyle="round")
        return
    if theme.border == "bevel":
        # Aluminium: a soft grey hairline outside, a white band, the grey body.
        hair, band = max(1.0, 0.9 * s), max(1.5, 2.4 * s)
        rounded(c, x0, y0, x1, y1, r, blend(theme.bg, "#000000", 0.18))
        rounded(c, x0 + hair, y0 + hair, x1 - hair, y1 - hair, max(0.0, r - hair), edge)
        k = hair + band
        rounded(c, x0 + k, y0 + k, x1 - k, y1 - k, max(0.0, r - k), theme.bg)
        return
    rounded(c, x0, y0, x1, y1, r, edge)
    rounded(c, x0 + e, y0 + e, x1 - e, y1 - e, max(0.0, r - e), theme.bg)
    if theme.border == "ornate":
        # A second, finer frame inside, and a diamond on each long side.
        g = 3.0 * s
        inner = blend(edge, theme.bg, 0.45)
        rounded(c, x0 + e + g, y0 + e + g, x1 - e - g, y1 - e - g, max(0.0, r - e - g), inner)
        f = max(1.0, 0.9 * s)
        rounded(c, x0 + e + g + f, y0 + e + g + f, x1 - e - g - f, y1 - e - g - f,
                max(0.0, r - e - g - f), theme.bg)
        mid = (x0 + x1) / 2
        d = 3.2 * s
        for y in (y0 + e / 2, y1 - e / 2):
            c.create_line(mid - 14 * s, y, mid + 14 * s, y, fill=theme.bg, width=e + 1)
            _diamond(c, mid, y, d, edge, edge)
            for side in (-1, 1):
                c.create_line(mid + side * 5 * s, y, mid + side * 13 * s, y, fill=edge, width=e)
    elif theme.border == "rule":
        # Newsprint: a heavy rule and a hairline inside the top and bottom edges.
        inset = max(r, 6 * s)
        for y, w in ((y0 + 3.2 * s, 2.0 * s), (y0 + 6.2 * s, 0.8 * s),
                     (y1 - 6.2 * s, 0.8 * s), (y1 - 3.2 * s, 2.0 * s)):
            c.create_line(x0 + inset, y, x1 - inset, y, fill=edge, width=max(1.0, w))


def _normals(pts):
    """Each outline point with its tangent and outward normal (clockwise outline)."""
    n = len(pts)
    for i, (px, py) in enumerate(pts):
        ax, ay = pts[i - 1]
        bx, by = pts[(i + 1) % n]
        tx, ty = bx - ax, by - ay
        d = math.hypot(tx, ty) or 1.0
        tx, ty = tx / d, ty / d
        yield px, py, tx, ty, ty, -tx


def _flame_border(c, theme: Theme, x0, y0, x1, y1, r, s, t) -> None:
    """Tongues of flame licking outward all round the pill, taller along the
    top (fire rises), never still; the body sits inside them."""
    m = 6.4 * s
    edge = theme.edge
    pts = _outline_points(x0 + m, y0 + m, x1 - m, y1 - m, max(0.0, r - m), 6.5 * s)
    layers = ((1.0, blend(edge, "#c81e00", 0.45)), (0.6, blend(edge, "#ffd060", 0.55)))
    for scale, colour in layers:
        for i, (px, py, tx, ty, nx, ny) in enumerate(_normals(pts)):
            flick = 0.5 + 0.5 * math.sin(t * 7.3 + i * 1.7) * math.sin(t * 4.1 + i * 0.83)
            up = 0.45 + 0.55 * max(0.0, -ny)
            hgt = (m - 0.4 * s) * up * (0.4 + 0.6 * flick) * scale
            bw = 4.6 * s * (0.7 + 0.3 * scale)
            sway = math.sin(t * 5.2 + i * 2.3) * 1.1 * s
            c.create_polygon(px - tx * bw, py - ty * bw, px + tx * bw, py + ty * bw,
                             px + nx * hgt + tx * sway, py + ny * hgt + ty * sway,
                             fill=colour, outline="")
    e = max(1.0, 1.4 * s)
    rounded(c, x0 + m, y0 + m, x1 - m, y1 - m, max(0.0, r - m), edge)
    rounded(c, x0 + m + e, y0 + m + e, x1 - m - e, y1 - m - e, max(0.0, r - m - e), theme.bg)


def _snowflake(c, cx: float, cy: float, size: float, colour: str, width: float,
               turn: float = 0.0) -> None:
    """Six arms, each with a pair of branches: the same all the way round."""
    for k in range(6):
        a = turn + k * math.pi / 3
        ex, ey = cx + size * math.cos(a), cy + size * math.sin(a)
        c.create_line(cx, cy, ex, ey, fill=colour, width=width, capstyle="round")
        bx, by = cx + size * 0.55 * math.cos(a), cy + size * 0.55 * math.sin(a)
        for side in (-1, 1):
            b = a + side * math.radians(42)
            c.create_line(bx, by, bx + size * 0.32 * math.cos(b), by + size * 0.32 * math.sin(b),
                          fill=colour, width=width, capstyle="round")


def _frost_border(c, theme: Theme, x0, y0, x1, y1, r, s, t) -> None:
    """Ice: a rimed edge, icicles hanging inside it, frost spikes along the
    bottom, and ice crystals set into the edge that glint in turn."""
    m = 4.4 * s
    edge = theme.edge
    e = max(1.0, 1.3 * s)
    bx0, by0, bx1, by1 = x0 + m, y0 + m, x1 - m, y1 - m
    rr = max(0.0, r - m)
    rounded(c, bx0, by0, bx1, by1, rr, edge)
    rounded(c, bx0 + e, by0 + e, bx1 - e, by1 - e, max(0.0, rr - e), theme.bg)
    g = 2.2 * s
    pts = _outline_points(bx0 + e + g, by0 + e + g, bx1 - e - g, by1 - e - g,
                          max(0.0, rr - e - g), 6 * s)
    flat = [v for p in pts for v in p]
    c.create_line(*flat, flat[0], flat[1], fill=blend(edge, theme.bg, 0.6),
                  width=max(1.0, 0.7 * s))
    rng = random.Random(7)  # frozen: the same icicles every frame
    left, right = bx0 + rr, bx1 - rr
    x = left + 4 * s
    while x < right - 4 * s:
        w = rng.uniform(1.0, 1.8) * s
        top = by0 + e
        c.create_polygon(x - w, top, x + w, top, x, top + rng.uniform(1.5, 4.5) * s,
                         fill=blend(edge, theme.bg, 0.3), outline="")
        bottom = by1 - e
        c.create_polygon(x - w * 0.7, bottom, x + w * 0.7, bottom,
                         x + rng.uniform(-0.8, 0.8) * s, bottom - rng.uniform(0.8, 2.4) * s,
                         fill=blend(edge, theme.bg, 0.5), outline="")
        x += rng.uniform(3.5, 7.0) * s
    span = right - left
    n = max(1, int(span / (55 * s)))
    for k in range(n + 1):
        cx = left + span * k / n
        for k2, cy in enumerate((by0, by1)):
            glint = 0.5 + 0.5 * math.sin(t * 2.4 + k * 1.9 + k2 * 2.8)
            c.create_oval(cx - m * 0.8, cy - m * 0.8, cx + m * 0.8, cy + m * 0.8, fill=theme.bg,
                          outline="")
            _snowflake(c, cx, cy, m * 0.98, blend(edge, "#ffffff", 0.6 * glint),
                       max(1.0, 0.8 * s), math.pi / 2)


def marker(theme: Theme) -> str:
    """The colour that outlines what a label clicks: it has to show up over
    any window, so the theme's strongest colour."""
    return theme.accent or theme.edge or theme.text


def label_box(c, theme: Theme, x0: float, y0: float, x1: float, y1: float, s: float,
              text: str = "", font=None) -> None:
    """A click label (or window chip, or grid number) in the theme's look:
    its colours, its font and a small version of its border."""
    if theme.pixel:
        pixel.label_box(c, theme, x0, y0, x1, y1, s, text)
        return
    edge = theme.edge or marker(theme)
    h = y1 - y0
    r = h / 2 * theme.round * (0.5 if theme.border in ("ornate", "rule") else 0.8)
    if theme.border == "rule":
        r = 0.0
    w1 = max(1.0, 1.1 * s) if h < 40 * s else max(2.0, 3 * s)
    if theme.border == "prism":
        rounded(c, x0, y0, x1, y1, r, theme.bg)
        pts = _outline_points(x0 + w1 / 2, y0 + w1 / 2, x1 - w1 / 2, y1 - w1 / 2,
                              max(0.0, r - w1 / 2), 3 * s)
        for i in range(len(pts)):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % len(pts)]
            c.create_line(ax, ay, bx, by, fill=hue(i / len(pts), 0.45, 1.0), width=w1 * 1.3)
    elif theme.border == "sketch":
        rounded(c, x0, y0, x1, y1, r, theme.bg)
        rng = random.Random(int(x0 * 7 + y0))
        flat = []
        for px, py in _outline_points(x0 + 1, y0 + 1, x1 - 1, y1 - 1, max(0.0, r - 1), 5 * s):
            flat += [px + rng.uniform(-0.8, 0.8) * s, py + rng.uniform(-0.8, 0.8) * s]
        c.create_line(*flat, flat[0], flat[1], fill=edge, width=w1, smooth=True)
    elif theme.border == "bevel":
        rounded(c, x0, y0, x1, y1, r, blend(theme.bg, "#000000", 0.25))
        rounded(c, x0 + 1, y0 + 1, x1 - 1, y1 - 1, max(0.0, r - 1), edge)
        k = 1 + w1 * 1.5
        rounded(c, x0 + k, y0 + k, x1 - k, y1 - k, max(0.0, r - k), theme.bg)
    else:
        rounded(c, x0, y0, x1, y1, r, edge)
        rounded(c, x0 + w1, y0 + w1, x1 - w1, y1 - w1, max(0.0, r - w1), theme.bg)
    k = h / (17 * s)  # decorations grow with the box (a screen number is huge)
    mid = (x0 + x1) / 2
    if theme.border == "ornate":
        # A diamond at each end, clear of the number.
        for x in (x0 + w1 / 2, x1 - w1 / 2):
            c.create_polygon(x, (y0 + y1) / 2 - 2.6 * s * k, x + 1.8 * s * k, (y0 + y1) / 2,
                             x, (y0 + y1) / 2 + 2.6 * s * k, x - 1.8 * s * k, (y0 + y1) / 2,
                             fill=edge, outline=theme.bg)
    elif theme.border == "rule":
        c.create_line(x0 + 2 * s * k, y1 - 2.2 * s * k, x1 - 2 * s * k, y1 - 2.2 * s * k,
                      fill=edge, width=max(1.0, 1.4 * s * k))
    elif theme.border == "flame":
        # A few tongues of flame rising off the top edge.
        n = max(2, int((x1 - x0) / (4.5 * s * k)))
        for i in range(n):
            cx = x0 + (x1 - x0) * (i + 0.5) / n
            tall = (2.0 + 1.6 * ((i * 7) % 3) / 2) * s * k
            for scale, col in ((1.0, blend(edge, "#c81e00", 0.4)), (0.55, blend(edge, "#ffd060", 0.5))):
                bw = 2.2 * s * k * scale
                c.create_polygon(cx - bw, y0 + w1, cx + bw, y0 + w1, cx, y0 + w1 - tall * scale - w1,
                                 fill=col, outline="")
    elif theme.border == "frost":
        # Icicles hanging inside the top edge.
        n = max(2, int((x1 - x0) / (5 * s * k)))
        for i in range(n):
            cx = x0 + (x1 - x0) * (i + 0.5) / n
            c.create_polygon(cx - 1.1 * s * k, y0 + w1, cx + 1.1 * s * k, y0 + w1,
                             cx, y0 + w1 + (1.0 + 0.6 * (i % 3)) * s * k,
                             fill=blend(edge, theme.bg, 0.3), outline="")
    if text:
        c.create_text(mid, (y0 + y1) / 2 + (0.8 * s * k if theme.border == "frost" else 0),
                      text=text, fill=theme.text, font=font)


def paste_burst(c, theme: Theme, cx: float, cy: float, s: float, p: float) -> None:
    """Where pasted text landed: a short burst in the theme's style, at
    progress ``p`` (0 .. 1). Fire: flames leap up. Ice: crystals fly out.
    Royal (stardust): gold sparkles. Gemstone: rainbow glints. Anything
    else: a ripple in its accent."""
    if theme.pixel:
        pixel.burst(c, theme, cx, cy, s, p)
        return
    ease = 1 - (1 - p) ** 3
    accent = theme.accent or theme.edge or theme.text
    if theme.dot == "flame" or theme.border == "flame":
        rng = random.Random(7)
        for k in range(9):
            dx = (k - 4) * 7 * s + rng.uniform(-3, 3) * s
            rise = (18 + rng.uniform(0, 34)) * s * ease
            h = (22 + rng.uniform(0, 16)) * s * (1 - p * 0.7)
            w = (6 + rng.uniform(0, 4)) * s * (1 - p * 0.5)
            base = cy + 6 * s - rise * 0.4
            sway = math.sin(p * 9 + k) * 3 * s
            for scale, col in ((1.0, blend(accent, "#c81e00", 0.35)), (0.55, "#ffd060")):
                c.create_polygon(cx + dx - w * scale, base, cx + dx + sway, base - h * scale - rise * 0.6,
                                 cx + dx + w * scale, base, fill=col, outline="", smooth=True)
        for k in range(6):  # embers
            a = rng.uniform(math.pi * 1.15, math.pi * 1.85)
            r = (20 + 50 * ease) * s
            ex, ey = cx + math.cos(a) * r, cy + math.sin(a) * r
            c.create_oval(ex - 1.6 * s, ey - 1.6 * s, ex + 1.6 * s, ey + 1.6 * s, fill="#ffb347",
                          outline="")
        return
    if theme.dot == "snowflake" or theme.border == "frost":
        r = (10 + 52 * ease) * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=blend(theme.edge or "#bfeaff",
                                                                    "#ffffff", 0.3),
                      width=max(1.0, 2.2 * s * (1 - p)))
        for k in range(6):
            a = k * math.pi / 3 + p * 0.8
            fx, fy = cx + math.cos(a) * r * 0.8, cy + math.sin(a) * r * 0.8
            _snowflake(c, fx, fy, (10 - 3 * p) * s, "#e6f8ff", max(1.0, 1.3 * s), a)
        _snowflake(c, cx, cy, (14 - 8 * p) * s, "#ffffff", max(1.2, 1.6 * s), p)
        return
    if theme.dot in ("stardust", "gem"):
        rng = random.Random(3)
        for k in range(10):
            a = k * 2 * math.pi / 10 + rng.uniform(-0.2, 0.2)
            r = (8 + rng.uniform(30, 60) * ease) * s
            size = (5 + rng.uniform(0, 6)) * s * (1 - p * 0.6) * (0.7 + 0.3 * math.sin(p * 12 + k))
            col = hue(k / 10 + p * 0.3, 0.5, 1.0) if theme.dot == "gem" else \
                blend(STARDUST["llm"], "#ffffff", rng.uniform(0, 0.6))
            _star(c, cx + math.cos(a) * r, cy + math.sin(a) * r, max(0.5, size), col)
        _star(c, cx, cy, (16 - 12 * p) * s, "#ffffff" if theme.dot == "gem" else STARDUST["llm"])
        return
    for k, lag in enumerate((0.0, 0.25)):  # the default: a ripple, like a tap
        q = max(0.0, min(1.0, (p - lag) / (1 - lag)))
        if q <= 0:
            continue
        r = (6 + 56 * (1 - (1 - q) ** 2)) * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=accent,
                      width=max(1.0, (3.0 - 1.2 * k) * s * (1 - q)))
    if p < 0.3:
        r = 6 * s * (1 - p / 0.3)
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=accent, outline="")


def recording_dot(c, theme: Theme, cx: float, cy: float, s: float, colour: str, t: float) -> None:
    """The pulsing "recording" mark, in the theme's style."""
    if theme.pixel:
        pixel.dot_at(c, theme, cx, cy, s, colour, t)
        return
    pulse = math.sin(t * 5)
    if theme.dot == "stardust":
        _stardust(c, cx, cy, s, colour, t)
        return
    if theme.dot == "gem":
        _gem(c, cx, cy, s, colour, t)
        return
    if theme.dot == "flame":
        _flame(c, cx, cy, s, colour, t)
        return
    if theme.dot == "snowflake":
        glow = 0.5 + 0.5 * math.sin(t * 2.0)
        r = (1.6 + 0.6 * glow) * s
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=blend(colour, "#ffffff", 0.5 * glow),
                      outline="")
        _snowflake(c, cx, cy, 7.2 * s, colour, max(1.2, 1.3 * s), t * 0.5)
        return
    if theme.dot == "cursor":
        if int(t * 2.5) % 2 == 0:  # a blinking block cursor
            c.create_rectangle(cx - 4 * s, cy - 6.5 * s, cx + 4 * s, cy + 6.5 * s, fill=colour,
                               outline=colour)
        return
    r = (4.5 + 1.2 * pulse) * s
    if theme.dot == "square":
        c.create_rectangle(cx - r, cy - r, cx + r, cy + r, fill=colour, outline=colour)
    elif theme.dot == "sketch":
        rng = random.Random(int(t * 4))
        pts = []
        for i in range(26):  # a circle scribbled round once and a bit, then filled in
            a = i / 22 * 2 * math.pi
            rr = r * (1 + rng.uniform(-0.12, 0.12))
            pts += [cx + rr * math.cos(a), cy + rr * math.sin(a)]
        c.create_line(*pts, fill=colour, width=max(1.0, 1.2 * s), smooth=True)
        for k in range(-2, 3):
            off = k * r * 0.35
            c.create_line(cx + off - r * 0.5, cy + r * 0.5, cx + off + r * 0.5, cy - r * 0.5,
                          fill=colour, width=max(1.0, 0.8 * s))
    else:
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=colour, outline=colour)


def _star(c, cx: float, cy: float, size: float, colour: str) -> None:
    """A four-pointed sparkle."""
    w = size * 0.24
    c.create_polygon(cx, cy - size, cx + w, cy - w, cx + size, cy, cx + w, cy + w,
                     cx, cy + size, cx - w, cy + w, cx - size, cy, cx - w, cy - w,
                     fill=colour, outline="")


def _stardust(c, cx: float, cy: float, s: float, colour: str, t: float) -> None:
    """A star in the middle, a ring of six sparkles and a ring of dust round
    it. Each part twinkles in its own time; the whole stays round."""
    glow = 0.5 + 0.5 * math.sin(t * 2.2)
    _star(c, cx, cy, (4.2 + 1.2 * glow) * s, blend(colour, "#ffffff", 0.35 * glow))
    for k in range(6):
        a = math.radians(30 + k * 60)
        tw = 0.5 + 0.5 * math.sin(t * 3.1 + k * 2.4)
        x, y = cx + 6.6 * s * math.cos(a), cy + 6.6 * s * math.sin(a)
        _star(c, x, y, (0.8 + 1.8 * tw) * s, blend(colour, "#ffffff", 0.5 * tw))
    for k in range(6):
        a = math.radians(k * 60)
        tw = 0.5 + 0.5 * math.sin(t * 4.3 + k * 1.7 + 1)
        x, y = cx + 8.4 * s * math.cos(a), cy + 8.4 * s * math.sin(a)
        r = (0.35 + 0.55 * tw) * s
        c.create_oval(x - r, y - r, x + r, y + r, fill=blend(colour, "#000000", 0.4 * (1 - tw)),
                      outline="")


def _flame(c, cx: float, cy: float, s: float, colour: str, t: float) -> None:
    """A candle flame: a teardrop with a hot core, its tip rising and falling
    (it doesn't lean, so it stays symmetrical)."""
    lift = (8.6 + 1.6 * math.sin(t * 6.1) * math.sin(t * 3.7 + 1)) * s
    for scale, shade, drop in ((1.0, colour, 0.0), (0.55, blend(colour, "#fff6d0", 0.65), 2.4)):
        y = cy + drop * s
        w, lo, up = 5.4 * s * scale, 6.8 * s * scale, lift * scale
        pts = [cx, y - up, cx + w * 0.55, y - up * 0.45, cx + w, y + 0.8 * s * scale,
               cx + w * 0.7, y + lo * 0.8, cx, y + lo, cx - w * 0.7, y + lo * 0.8,
               cx - w, y + 0.8 * s * scale, cx - w * 0.55, y - up * 0.45]
        c.create_polygon(*pts, fill=shade, outline="", smooth=True)


def _gem(c, cx: float, cy: float, s: float, colour: str, t: float) -> None:
    """A brilliant-cut gem seen from above: an octagon, its table, and
    facets that catch the light in turn, with flashes of rainbow fire."""
    R, r = 7.6 * s, 3.9 * s
    outer = [(cx + R * math.cos(math.radians(22.5 + k * 45)),
              cy + R * math.sin(math.radians(22.5 + k * 45))) for k in range(8)]
    inner = [(cx + r * math.cos(math.radians(k * 45)), cy + r * math.sin(math.radians(k * 45)))
             for k in range(8)]
    c.create_polygon(*[v for p in outer for v in p], fill=blend(colour, "#000000", 0.3),
                     outline=blend(colour, "#ffffff", 0.6), width=max(1.0, 0.7 * s))
    for k in range(8):  # star facets between the table and the girdle
        lit = 0.5 + 0.5 * math.sin(t * 2.6 - k * math.pi / 4)
        shade = blend(colour, "#ffffff", 0.55 * lit) if k % 2 else blend(colour, "#000000", 0.25 * (1 - lit))
        p1, p2, q = outer[k], outer[(k + 1) % 8], inner[(k + 1) % 8]
        c.create_polygon(*p1, *p2, *q, fill=shade, outline="")
    c.create_polygon(*[v for p in inner for v in p], fill=blend(colour, "#ffffff", 0.25),
                     outline=blend(colour, "#ffffff", 0.7), width=1)
    for k in range(4):  # fire: little rainbow flashes at the corners
        phase = math.sin(t * 3.0 + k * 1.9)
        if phase > 0.55:
            x, y = outer[k * 2]
            _star(c, x, y, 2.4 * s * (phase - 0.4), hue(t * 0.3 + k / 4, 0.6, 1.0))


def level_height(level: float) -> float:
    """0-1 for a level, on a -60 dB .. -20 dB scale."""
    db = 20 * math.log10(max(level, 1e-5))
    return min(max((db + 60) / 40, 0.0), 1.0)


_GLYPHS = "01ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄ0123456789Z:."


def wave(c, theme: Theme, levels, x0: float, x1: float, mid: float, max_h: float, s: float,
         speech: float, t: float = 0.0) -> None:
    """The live level meter, newest on the right, in the theme's style."""
    if theme.pixel:
        pixel.wave_at(c, theme, levels, x0, x1, mid, max_h, s, speech, t)
        return
    levels = list(levels)
    n = len(levels) or 1
    step = (x1 - x0) / n
    style = theme.wave_style
    loud_now = any(level >= speech for level in levels[-6:])
    if style == "flames":
        # A row of flames, tall as the voice is loud, flickering, hot at the core.
        base = mid + max_h / 2
        for i, level in enumerate(levels):
            lh = level_height(level)
            cx = x0 + i * step + step / 2
            hh = max(1.5 * s, lh * max_h * (0.85 + 0.15 * math.sin(t * 11 + i * 1.9)))
            sway = math.sin(t * 8 + i * 1.3) * step * 0.22
            bw = step * 0.62
            if level >= speech:
                layers = ((1.0, blend(theme.wave, "#c81e00", 0.4 * lh)),
                          (0.55, blend(theme.wave, "#ffe08a", 0.6)))
            else:
                layers = ((1.0, theme.dim),)
            for scale, colour in layers:
                h2, w2 = hh * scale, bw * (0.55 + 0.45 * scale)
                if h2 < 1.5 * s:
                    continue
                c.create_polygon(cx - w2, base, cx - w2 * 0.6, base - h2 * 0.45,
                                 cx + sway * scale, base - h2, cx + w2 * 0.6, base - h2 * 0.45,
                                 cx + w2, base, fill=colour, outline="", smooth=True)
        return
    if style == "wind":
        # An icy wind: three streams blowing through, stirred as you speak,
        # with snow carried along on it.
        colour = theme.wave if loud_now else theme.dim
        for k in range(3):
            pts = []
            for i, level in enumerate(levels):
                a = level_height(level) * max_h * 0.32 * (1 - 0.3 * abs(k - 1))
                pts += [x0 + i * step + step / 2,
                        mid + (k - 1) * max_h * 0.2 + a * math.sin(i * 0.55 - t * 5 + k * 2.1)]
            if len(pts) >= 4:
                c.create_line(x0, mid + (k - 1) * max_h * 0.2, *pts, smooth=True, splinesteps=6,
                              fill=colour if k == 1 else blend(colour, theme.bg, 0.45),
                              width=max(1.0, (1.4 if k == 1 else 0.9) * s), capstyle="round")
        span = x1 - x0
        for j in range(7):
            x = x1 - ((t * (30 + j * 3) * s + j * span / 7) % span)
            y = mid + math.sin(t * 1.8 + j * 1.7) * max_h * 0.42
            r = (0.7 + 0.25 * (j % 3)) * s
            c.create_oval(x - r, y - r, x + r, y + r, fill=colour, outline="")
        return
    if style in ("line", "zigzag", "scribble"):
        pts = []
        for i, level in enumerate(levels):
            amp = max(0.6 * s, level_height(level) * max_h / 2)
            pts += [x0 + i * step + step / 2, mid + (amp if i % 2 else -amp)]
        colour = theme.wave if loud_now else theme.dim
        if len(pts) < 4:
            return
        if style == "line":
            c.create_line(x0, mid, *pts, x1, mid, fill=colour, width=max(1.5, 1.6 * s),
                          smooth=True, splinesteps=8, capstyle="round", joinstyle="round")
        elif style == "zigzag":
            # Straight strokes, crisp points: a heart monitor, not a ribbon.
            c.create_line(x0, mid, x1, mid, fill=theme.dim, width=max(1.0, 0.8 * s))
            c.create_line(x0, mid, *pts, x1, mid, fill=colour, width=max(1.2, 1.4 * s),
                          joinstyle="miter")
            return
        else:  # scribble: two pencil passes that wobble
            rng = random.Random(int(t * 6))
            for k in range(2):
                wob = [p + rng.uniform(-1.4, 1.4) * s for p in pts]
                c.create_line(x0, mid, *wob, x1, mid, smooth=True, splinesteps=6,
                              fill=colour if k == 0 else blend(colour, theme.bg, 0.45),
                              width=max(1.0, (1.5 - 0.5 * k) * s), capstyle="round")
        c.create_line(x0, mid, x1, mid, fill=theme.dim, width=1)
        return
    for i, level in enumerate(levels):
        h = max(2 * s, level_height(level) * max_h)
        x = x0 + i * step
        cx = x + step / 2
        colour = theme.wave if level >= speech else theme.dim
        if style == "capsules":
            # Soft rounded capsules, heights eased between neighbours.
            prev = level_height(levels[i - 1]) if i else level_height(level)
            hh = max(2.2 * s, (0.35 * prev + 0.65 * level_height(level)) * max_h * 0.9)
            bw = max(2.0, step * 0.5)
            c.create_line(cx, mid - hh / 2 + bw / 2, cx, mid + hh / 2 - bw / 2, fill=colour,
                          width=bw, capstyle="round")
        elif style == "hairlines":
            # Fine lines, eased between neighbours, mirrored round the middle.
            prev = level_height(levels[i - 1]) if i else level_height(level)
            hh = max(1.5 * s, (0.4 * prev + 0.6 * level_height(level)) * max_h * 0.95)
            c.create_line(cx, mid - hh / 2, cx, mid + hh / 2, fill=colour,
                          width=max(1.0, 1.1 * s), capstyle="round")
        elif style == "prism":
            # Light split into its colours, drifting along as you talk.
            col = hue(i / n * 0.85 + t * 0.12, 0.5, 1.0) if level >= speech else theme.dim
            bw = max(2, int(step * 0.55))
            c.create_rectangle(x, mid - h / 2, x + bw, mid + h / 2, fill=col, outline=col)
        elif style == "dots":
            r = max(1.2 * s, h / 4)
            c.create_oval(cx - r, mid - r, cx + r, mid + r, fill=colour, outline=colour)
        elif style == "dotgrid":
            # A dot-matrix meter: five dots a column, lit from the middle out.
            rows, gap = 5, max_h / 5
            lit = round(level_height(level) * rows)
            for j in range(rows):
                y = mid + (j - (rows - 1) / 2) * gap
                on = abs(j - (rows - 1) / 2) * 2 < max(1, lit)
                col = theme.wave if on and level >= speech else theme.dim
                r = 1.3 * s if not on else 1.7 * s
                c.create_oval(cx - r, y - r, cx + r, y + r, fill=col, outline=col)
        elif style == "halftone":
            # Newsprint: a stack of dots that shrink away from the middle.
            rows, gap = 5, max_h / 5
            for j in range(rows):
                y = mid + (j - (rows - 1) / 2) * gap
                k = 1 - abs(j - (rows - 1) / 2) / ((rows - 1) / 2 + 0.6)
                r = max(0.4 * s, level_height(level) * k * step * 0.5)
                c.create_oval(cx - r, y - r, cx + r, y + r, fill=colour, outline=colour)
        elif style == "glyphs":
            rows = max(1, round(level_height(level) * 4))
            size = max(7, int(7.5 * s))
            gap = max_h / 4
            for j in range(rows):
                y = mid + (j - (rows - 1) / 2) * gap
                ch = _GLYPHS[(i * 7 + j * 3 + int(t * 8)) % len(_GLYPHS)]
                col = theme.wave if j == rows - 1 and level >= speech else colour
                c.create_text(cx, y, text=ch, fill=col, font=("MS Gothic", -size))
        else:
            bw = max(2, int(step * 0.55))
            c.create_rectangle(x, mid - h / 2, x + bw, mid + h / 2, fill=colour, outline=colour)


def preview_levels(t: float, n: int = 28) -> list[float]:
    """Speech-like levels for a preview: syllables rising and falling."""
    out = []
    for i in range(n):
        x = t * 6 - (n - i) * 0.45
        env = 0.5 + 0.5 * math.sin(x * 0.9) * math.sin(x * 0.37 + 1)
        out.append(10 ** (-60 / 20 + 40 / 20 * max(0.0, env)) * (1 if env > 0.15 else 0.2))
    return out


def with_changes(theme: Theme, **changes) -> Theme:
    return replace(theme, **changes)
