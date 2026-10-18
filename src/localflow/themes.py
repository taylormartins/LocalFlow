"""Pill themes: colours, font and waveform style for the overlay.

Built-in: "default" (the original pill) and "royal" (black and gold, a
flowing line for the waveform, script lettering). Your own are made in
Settings > Theme builder and kept in themes.json next to config.toml (this PC
only). The pill and the Settings preview draw through the same functions
here, so the preview is exactly what the pill will look like.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, fields, replace

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

STORE = PROJECT_ROOT / "themes.json"

WAVE_STYLES = ("bars", "line", "dots")


@dataclass
class Theme:
    name: str = "Default"
    #: Pill background, text, and an outline round the pill ("" = none).
    bg: str = "#1d1e22"
    text: str = "#ececee"
    edge: str = ""
    #: The waveform: loud parts, quiet parts.
    wave: str = "#ececee"
    dim: str = "#4b4d55"
    #: bars | line | dots
    wave_style: str = "bars"
    font: str = "Segoe UI"
    font_size: int = 13
    #: Replaces the state colours (working, done, command mode) when set.
    #: Errors stay red either way: that colour carries meaning.
    accent: str = ""


DEFAULT = Theme()
ROYAL = Theme(
    name="Royal", bg="#0b0a08", text="#e9c96b", edge="#c9a227", wave="#e2bd4f",
    dim="#5b4a1c", wave_style="line", font="Segoe Script", font_size=12, accent="#d4af37",
)
BUILT_IN = {"default": DEFAULT, "royal": ROYAL}


def _from_dict(name: str, d: dict) -> Theme:
    known = {f.name for f in fields(Theme)}
    t = Theme(**{k: v for k, v in d.items() if k in known})
    t.name = name
    if t.wave_style not in WAVE_STYLES:
        t.wave_style = "bars"
    try:
        t.font_size = max(8, min(int(t.font_size), 24))
    except (TypeError, ValueError):
        t.font_size = 13
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


def resolve(name: str) -> Theme:
    return all_themes().get((name or "default").lower(), DEFAULT)


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


# -- drawing (the pill and the Settings preview) ------------------------------

def rounded(c, x0: float, y0: float, x1: float, y1: float, r: float, fill: str) -> None:
    r = max(0.0, min(r, (y1 - y0) / 2, (x1 - x0) / 2))
    c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill)
    c.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, outline=fill)
    for cx, cy in ((x0 + r, y0 + r), (x1 - r, y0 + r), (x0 + r, y1 - r), (x1 - r, y1 - r)):
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline=fill)


def pill(c, theme: Theme, x0: float, y0: float, x1: float, y1: float, r: float, s: float) -> None:
    """The pill's body, with its outline if the theme has one."""
    if theme.edge:
        e = max(1.0, 1.5 * s)
        rounded(c, x0, y0, x1, y1, r, theme.edge)
        rounded(c, x0 + e, y0 + e, x1 - e, y1 - e, r - e, theme.bg)
    else:
        rounded(c, x0, y0, x1, y1, r, theme.bg)


def level_height(level: float) -> float:
    """0-1 for a level, on a -60 dB .. -20 dB scale."""
    db = 20 * math.log10(max(level, 1e-5))
    return min(max((db + 60) / 40, 0.0), 1.0)


def wave(c, theme: Theme, levels, x0: float, x1: float, mid: float, max_h: float, s: float,
         speech: float) -> None:
    """The live level meter, newest on the right, in the theme's style."""
    levels = list(levels)
    n = len(levels) or 1
    step = (x1 - x0) / n
    if theme.wave_style == "line":
        # One flowing line, swinging up and down by the level: a waveform.
        pts = []
        for i, level in enumerate(levels):
            amp = max(0.6 * s, level_height(level) * max_h / 2)
            pts += [x0 + i * step + step / 2, mid + (amp if i % 2 else -amp)]
        loud = any(level >= speech for level in levels[-6:])
        if len(pts) >= 4:
            c.create_line(x0, mid, *pts, x1, mid, fill=theme.wave if loud else theme.dim,
                          width=max(1.5, 1.6 * s), smooth=True, splinesteps=8,
                          capstyle="round", joinstyle="round")
        c.create_line(x0, mid, x1, mid, fill=theme.dim, width=1)
        return
    for i, level in enumerate(levels):
        h = max(2 * s, level_height(level) * max_h)
        x = x0 + i * step
        colour = theme.wave if level >= speech else theme.dim
        if theme.wave_style == "dots":
            r = max(1.2 * s, h / 4)
            cx = x + step / 2
            c.create_oval(cx - r, mid - r, cx + r, mid + r, fill=colour, outline=colour)
        else:
            bw = max(2, int(step * 0.55))
            c.create_rectangle(x, mid - h / 2, x + bw, mid + h / 2, fill=colour, outline=colour)


def colour_for(theme: Theme, state_colour: str, error_colour: str) -> str:
    """A state's colour under this theme: the accent, except errors."""
    if theme.accent and state_colour != error_colour:
        return theme.accent
    return state_colour


#: The recording dot: LLM cleanup, algorithmic cleanup, raw (no cleanup).
DOTS = {"llm": "#43b974", "rules": "#4c9aff", "raw": None}


def dot_colour(theme: Theme, kind: str) -> str:
    """The recording dot. Its three meanings stay apart under an accent:
    the accent, a darker accent, the text colour."""
    if kind == "raw":
        return theme.text
    if theme.accent:
        return theme.accent if kind == "llm" else blend(theme.accent, theme.bg, 0.5)
    return DOTS[kind]


def font(theme: Theme, s: float, bold: bool = False, size: int | None = None) -> tuple:
    px = -int((size or theme.font_size) * s)
    if bold and theme.font == "Segoe UI":
        return ("Segoe UI Semibold", px)
    return (theme.font, px, "bold") if bold else (theme.font, px)


def preview_levels(t: float, n: int = 28) -> list[float]:
    """Speech-like levels for a preview: syllables rising and falling."""
    out = []
    for i in range(n):
        x = t * 6 - (n - i) * 0.45
        env = 0.5 + 0.5 * math.sin(x * 0.9) * math.sin(x * 0.37 + 1)
        out.append(10 ** (-60 / 20 + 40 / 20 * max(0.0, env)) * (1 if env > 0.15 else 0.2))
    return out


def blend(a: str, b: str, amount: float) -> str:
    """``a`` moved ``amount`` of the way towards ``b`` (#rrggbb colours)."""
    try:
        ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
        cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return a
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(ca, cb))


def with_changes(theme: Theme, **changes) -> Theme:
    return replace(theme, **changes)
