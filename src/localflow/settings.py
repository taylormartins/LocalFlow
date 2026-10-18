"""The Settings window: everything that isn't needed day to day.

Black and gold. Tabs: Appearance (pick the pill's theme), Theme builder
(make your own), Dictation, Listening, Sounds. It runs as its own process,
like the history window. Every change is saved to config.toml (or
themes.json / learned.json) at once, and the running tray app notices the
file change and applies it within a second (tray.Tray.apply_settings).
"""

from __future__ import annotations

import logging
import threading
import time

from .config import CONFIG_PATH, Config, save_setting

log = logging.getLogger(__name__)

# The window's own black and gold.
BLACK = "#0d0c0a"
PANEL = "#16140f"
FIELD = "#1f1c15"
LINE = "#3b3322"
GOLD = "#d4af37"
GOLD_HI = "#f0cf63"
TEXT = "#efe6cf"
MUTED = "#9d916f"

TIMEOUTS = {
    "command": [15, 30, 60, 120, 300, 600, 0],
    "dictation": [30, 45, 60, 120, 300, 0],
}
SUGGESTED_FONTS = ["Segoe UI", "Segoe Script", "Gabriola", "Lucida Handwriting", "Ink Free",
                   "Monotype Corsiva", "Georgia", "Palatino Linotype", "Cascadia Code",
                   "Consolas", "Bahnschrift", "Comic Sans MS"]


def _save(section: str, key: str, value) -> None:
    try:
        save_setting(section, key, value)
    except OSError as exc:
        log.warning("could not save %s.%s: %s", section, key, exc)


def _style(root) -> None:
    from tkinter import ttk

    st = ttk.Style(root)
    st.theme_use("clam")
    base = ("Segoe UI", 10)
    st.configure(".", background=BLACK, foreground=TEXT, fieldbackground=FIELD, font=base,
                 bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, troughcolor=FIELD,
                 selectbackground=GOLD, selectforeground=BLACK, insertcolor=GOLD,
                 focuscolor=GOLD)
    st.configure("TFrame", background=BLACK)
    st.configure("Card.TFrame", background=PANEL, bordercolor=LINE, relief="solid", borderwidth=1)
    st.configure("TLabel", background=BLACK, foreground=TEXT)
    st.configure("Card.TLabel", background=PANEL, foreground=TEXT)
    st.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
    st.configure("Head.TLabel", background=PANEL, foreground=GOLD, font=("Georgia", 12, "bold"))
    st.configure("Title.TLabel", background=BLACK, foreground=GOLD, font=("Georgia", 18, "bold"))
    st.configure("Sub.TLabel", background=BLACK, foreground=MUTED, font=("Segoe UI", 9))
    st.configure("TNotebook", background=BLACK, borderwidth=0, tabmargins=(8, 6, 8, 0))
    st.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(14, 6),
                 bordercolor=LINE, font=("Segoe UI Semibold", 10))
    st.map("TNotebook.Tab", background=[("selected", BLACK)],
           foreground=[("selected", GOLD), ("active", GOLD_HI)])
    for w in ("TCheckbutton", "TRadiobutton"):
        # Gold when on: clam fills the indicator with indicatorbackground.
        st.configure(w, background=PANEL, foreground=TEXT, indicatorbackground=FIELD,
                     indicatorforeground=BLACK, indicatormargin=(0, 0, 6, 0),
                     upperbordercolor=GOLD, lowerbordercolor=GOLD)
        st.map(w, indicatorbackground=[("selected", GOLD), ("pressed", GOLD_HI)],
               background=[("active", PANEL)], foreground=[("active", GOLD_HI)])
    st.configure("TButton", background=FIELD, foreground=GOLD, bordercolor=GOLD,
                 padding=(10, 4), font=("Segoe UI Semibold", 10))
    st.map("TButton", background=[("active", GOLD), ("pressed", GOLD_HI)],
           foreground=[("active", BLACK), ("pressed", BLACK)])
    st.configure("Small.TButton", padding=(8, 1), font=("Segoe UI Semibold", 9))
    st.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BLACK, bordercolor=BLACK,
                 arrowcolor=GOLD, lightcolor=FIELD, darkcolor=FIELD)
    st.map("Vertical.TScrollbar", background=[("active", LINE)])
    st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                 arrowcolor=GOLD, bordercolor=LINE)
    st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
           selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
    st.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, bordercolor=LINE)
    st.configure("TSpinbox", fieldbackground=FIELD, foreground=TEXT, arrowcolor=GOLD,
                 bordercolor=LINE, background=FIELD)
    st.configure("Horizontal.TScale", background=GOLD, troughcolor=FIELD, bordercolor=LINE)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", GOLD)
    root.option_add("*TCombobox*Listbox.selectForeground", BLACK)
    root.option_add("*TCombobox*Listbox.font", base)


class Preview:
    """The pill as it will look, animated: recording, then a message."""

    def __init__(self, parent, get_theme, width: int = 250, height: int = 42):
        import tkinter as tk

        self.get_theme = get_theme
        self.w, self.h = width, height
        self.canvas = tk.Canvas(parent, width=width + 24, height=2 * height + 36, bg=PANEL,
                                highlightthickness=0)
        self.t0 = time.monotonic()
        self._tick()

    def _tick(self) -> None:
        from . import themes

        try:
            self.draw(themes)
        except Exception:
            log.debug("preview failed", exc_info=True)
        self.canvas.after(50, self._tick)

    def draw(self, themes) -> None:
        c, w, h, s = self.canvas, self.w, self.h, 1.0
        theme = self.get_theme()
        t = time.monotonic() - self.t0
        c.delete("all")
        x, y = 12, 12
        # Recording.
        now = time.monotonic()
        themes.pill(c, theme, x, y, x + w, y + h, h / 2, s, now)
        mid = y + h / 2
        themes.recording_dot(c, theme, x + 22, mid, s, themes.dot_colour(theme, "llm"), t)
        themes.wave(c, theme, themes.preview_levels(t), x + 38, x + w - 58, mid, h - 16, s, 0.009,
                    now)
        secs = int(t) % 60
        c.create_text(x + w - 18, mid, text=f"0:{secs:02d}", anchor="e", fill=theme.text,
                      font=themes.font(theme, s))
        # A message.
        y2 = y + h + 12
        themes.pill(c, theme, x, y2, x + w, y2 + h, h / 2, s, now)
        mid2 = y2 + h / 2
        colour = themes.colour_for(theme, "#43b974", "#ef5350")
        cx = x + 24
        c.create_oval(cx - 8, mid2 - 8, cx + 8, mid2 + 8, fill=colour, outline=colour)
        c.create_line(cx - 4, mid2, cx - 1, mid2 + 3.5, cx + 4.5, mid2 - 3.5, fill=theme.bg,
                      width=2)
        c.create_text(x + 40, mid2, text="Inserted", anchor="w", fill=theme.text,
                      font=themes.font(theme, s))


def _card(parent, title: str, note: str = ""):
    from tkinter import ttk

    card = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
    card.pack(fill="x", padx=14, pady=(10, 0))
    ttk.Label(card, text=title, style="Head.TLabel").pack(anchor="w")
    if note:
        ttk.Label(card, text=note, style="Muted.TLabel", wraplength=560,
                  justify="left").pack(anchor="w", pady=(0, 6))
    return card


def _row(card, label: str, width: int = 22):
    from tkinter import ttk

    row = ttk.Frame(card, style="Card.TFrame", padding=0)
    row.configure(borderwidth=0)
    row.pack(fill="x", pady=3)
    ttk.Label(row, text=label, style="Card.TLabel", width=width).pack(side="left")
    return row


def _choice(row, values: list[str], current: str, on_pick, width: int = 34):
    import tkinter as tk
    from tkinter import ttk

    var = tk.StringVar(value=current)
    box = ttk.Combobox(row, textvariable=var, values=values, state="readonly", width=width)
    box.pack(side="left")
    box.bind("<<ComboboxSelected>>", lambda _e: on_pick(var.get()))
    return var, box


def _check(card, text: str, value: bool, on_change):
    import tkinter as tk
    from tkinter import ttk

    var = tk.BooleanVar(value=value)
    ttk.Checkbutton(card, text=text, variable=var,
                    command=lambda: on_change(var.get())).pack(anchor="w", pady=2)
    return var


def _scroller(nb, title: str):
    """A tab whose contents scroll."""
    import tkinter as tk
    from tkinter import ttk

    outer = ttk.Frame(nb)
    nb.add(outer, text=title)
    canvas = tk.Canvas(outer, bg=BLACK, highlightthickness=0)
    bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    def wheel(e):
        if str(nb.select()) == str(outer):
            canvas.yview_scroll(int(-e.delta / 120), "units")

    canvas.bind_all("<MouseWheel>", wheel, add="+")
    return inner


def _say_seconds(secs: float) -> str:
    from .commands import say_seconds

    return say_seconds(secs).capitalize()


# -- the tabs ------------------------------------------------------------------

def _appearance(nb, cfg: Config, state: dict) -> None:
    import tkinter as tk
    from tkinter import ttk

    from . import themes

    tab = _scroller(nb, "Appearance")
    card = _card(tab, "Theme", "How the pill looks. It changes as soon as you pick one.")
    body = ttk.Frame(card, style="Card.TFrame")
    body.configure(borderwidth=0)
    body.pack(fill="x")
    left = ttk.Frame(body, style="Card.TFrame")
    left.configure(borderwidth=0)
    left.pack(side="left", fill="y", padx=(0, 16))
    current = tk.StringVar(value=cfg.overlay.theme.lower())
    state["theme_var"] = current

    def names():
        return {k: t.name for k, t in themes.all_themes().items()}

    def pick():
        _save("overlay", "theme", current.get())

    def fill():
        for w in left.winfo_children():
            w.destroy()
        for key, name in names().items():
            built = "  (built in)" if key in themes.BUILT_IN else ""
            ttk.Radiobutton(left, text=name + built, value=key, variable=current,
                            command=pick).pack(anchor="w", pady=2)

    fill()
    state["refresh_themes"] = fill
    prev = Preview(body, lambda: themes.resolve(current.get()))
    prev.canvas.pack(side="left")

    card = _card(tab, "Pill opacity", "Lower it to see what's behind the pill.")
    row = _row(card, "Pill opacity")
    op = tk.DoubleVar(value=cfg.overlay.opacity)
    op_scale = ttk.Scale(row, from_=0.2, to=1.0, variable=op, length=240)
    op_scale.pack(side="left")
    op_shown = ttk.Label(row, text=f"{op.get():.0%}", style="Card.TLabel")
    op_shown.pack(side="left", padx=8)

    def op_done(_e=None):
        op_shown.configure(text=f"{op.get():.0%}")
        _save("overlay", "opacity", round(op.get(), 2))

    op_scale.bind("<ButtonRelease-1>", op_done)
    op_scale.configure(command=lambda _v: op_shown.configure(text=f"{op.get():.0%}"))

    card = _card(tab, "Click labels", '"Show labels" numbers, the window list and the grid: '
                 'their look, and how solid they are.')
    looks = dict(themes.LABEL_CHOICES)
    looks.update({k: t.name for k, t in themes.all_themes().items()})
    by_name = {v: k for k, v in looks.items()}
    row = _row(card, "Label look")
    look_var, _ = _choice(row, list(looks.values()),
                          looks.get(cfg.overlay.label_theme.lower(), looks["match"]),
                          lambda name: _save("overlay", "label_theme", by_name[name]))
    LABEL_MIN = themes.LABEL_MIN
    row = _row(card, "Label size")
    size_var = tk.IntVar(value=max(LABEL_MIN, min(int(cfg.overlay.label_size), 28)))
    size_scale = ttk.Scale(row, from_=LABEL_MIN, to=28, length=240,
                           command=lambda v: size_var.set(round(float(v))))
    size_scale.set(size_var.get())
    size_scale.pack(side="left")
    size_shown = ttk.Label(row, textvariable=size_var, style="Card.TLabel")
    size_shown.pack(side="left", padx=8)
    size_scale.bind("<ButtonRelease-1>",
                    lambda _e: _save("overlay", "label_size", int(size_var.get())))
    sample = tk.Canvas(card, width=320, height=76, bg=PANEL, highlightthickness=0)
    sample.pack(anchor="w", pady=(6, 2))

    def draw_sample():
        """Two labels on pretend buttons and a window's number, as they'll look."""
        try:
            theme = themes.label_theme(by_name.get(look_var.get(), "match"),
                                       themes.resolve(current.get()))
            sample.delete("all")
            mark = themes.marker(theme)
            import tkinter.font as tkfont

            bold = themes.font(theme, 1.0, bold=True, size=int(size_var.get()))
            m = tkfont.Font(root=sample, font=bold)
            bh = m.metrics("linespace") + 3
            for i, (x, n) in enumerate(((8, "3"), (115, "12"))):
                sample.create_rectangle(x, 8, x + 96, 68, fill="#f4f4f4" if i else "#2b2d33",
                                        outline=mark)
                bw = max(bh, m.measure(n) + 9)
                themes.label_box(sample, theme, x + 2, 10, x + 2 + bw, 10 + bh, 1.0, n, bold)
            themes.label_box(sample, theme, 228, 8, 280, 68, 1.0, "7",
                             themes.font(theme, 1.0, bold=True, size=32))
        except Exception:
            log.debug("label sample failed", exc_info=True)
        sample.after(400, draw_sample)

    draw_sample()
    row = _row(card, "Label opacity")
    var = tk.DoubleVar(value=cfg.overlay.label_opacity)
    scale = ttk.Scale(row, from_=0.3, to=1.0, variable=var, length=240)
    scale.pack(side="left")
    shown = ttk.Label(row, text=f"{var.get():.0%}", style="Card.TLabel")
    shown.pack(side="left", padx=8)

    def done(_e=None):
        shown.configure(text=f"{var.get():.0%}")
        _save("overlay", "label_opacity", round(var.get(), 2))

    scale.bind("<ButtonRelease-1>", done)


def _builder(nb, cfg: Config, state: dict) -> None:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import colorchooser, ttk

    from . import themes

    tab = _scroller(nb, "Theme builder")
    work = {"theme": themes.with_changes(themes.resolve(cfg.overlay.theme))}
    if work["theme"].name.lower() in themes.BUILT_IN:
        work["theme"] = themes.with_changes(work["theme"], name="My " + work["theme"].name)

    card = _card(tab, "Build your own", "Start from any theme, change what you like, save it "
                 "under a name. The preview is the real pill.")
    prev = Preview(card, lambda: work["theme"])
    prev.canvas.pack(anchor="w", pady=(0, 6))
    controls = ttk.Frame(card, style="Card.TFrame")
    controls.configure(borderwidth=0)
    controls.pack(fill="x")

    def set_(field: str, value) -> None:
        work["theme"] = themes.with_changes(work["theme"], **{field: value})

    row = _row(controls, "Start from", 16)
    base_var, base_box = _choice(row, [], themes.resolve(cfg.overlay.theme).name,
                                 lambda v: start_from(v), width=22)

    def bases():
        return {t.name: k for k, t in themes.all_themes().items()}

    base_box.configure(values=list(bases()))

    row = _row(controls, "Name", 16)
    name_var = tk.StringVar(value=work["theme"].name)
    ttk.Entry(row, textvariable=name_var, width=24).pack(side="left")

    swatches: dict[str, tk.Label] = {}

    def colour_row(field: str, label: str, optional: bool = False):
        row = _row(controls, label, 16)
        sw = tk.Label(row, width=6, relief="solid", bd=1, cursor="hand2")
        sw.pack(side="left")
        swatches[field] = sw

        def choose(_e=None):
            start = getattr(work["theme"], field) or "#d4af37"
            got = colorchooser.askcolor(color=start, title=label, parent=nb)[1]
            if got:
                set_(field, got)
                paint()

        sw.bind("<Button-1>", choose)
        ttk.Button(row, text="Pick", command=choose, style="Small.TButton").pack(side="left",
                                                                                padx=6)
        if optional:
            ttk.Button(row, text="None", command=lambda: (set_(field, ""), paint()),
                       style="Small.TButton").pack(side="left")

    colour_row("bg", "Background")
    colour_row("text", "Text")
    colour_row("edge", "Outline", optional=True)
    colour_row("wave", "Waveform")
    colour_row("dim", "Waveform, quiet")
    colour_row("accent", "Accent (states)", optional=True)

    row = _row(controls, "Font", 16)
    have = set(tkfont.families(nb))
    fonts = [f for f in SUGGESTED_FONTS if f in have]
    fonts += sorted(f for f in have if f not in fonts and not f.startswith("@"))
    font_var, _ = _choice(row, fonts, work["theme"].font, lambda v: set_("font", v), width=22)
    row = _row(controls, "Text size", 16)
    size_var = tk.IntVar(value=work["theme"].font_size)
    ttk.Spinbox(row, from_=8, to=24, textvariable=size_var, width=5,
                command=lambda: set_("font_size", size_var.get())).pack(side="left")
    row = _row(controls, "Waveform", 16)
    wave_var, _ = _choice(row, list(themes.WAVE_STYLES), work["theme"].wave_style,
                          lambda v: set_("wave_style", v), width=14)
    row = _row(controls, "Border", 16)
    border_var, _ = _choice(row, list(themes.BORDERS), work["theme"].border,
                            lambda v: set_("border", v), width=14)
    row = _row(controls, "Recording mark", 16)
    dot_var, _ = _choice(row, list(themes.DOT_STYLES), work["theme"].dot,
                         lambda v: set_("dot", v), width=14)
    row = _row(controls, "Corners", 16)
    round_var = tk.DoubleVar(value=work["theme"].round)
    ttk.Scale(row, from_=0.0, to=1.0, variable=round_var, length=160,
              command=lambda _v: set_("round", round(round_var.get(), 2))).pack(side="left")
    ttk.Label(row, text="  square ... round", style="Muted.TLabel").pack(side="left")

    def paint():
        t = work["theme"]
        for field, sw in swatches.items():
            v = getattr(t, field)
            sw.configure(bg=v or PANEL, text="" if v else "none", fg=MUTED)
        font_var.set(t.font)
        size_var.set(t.font_size)
        wave_var.set(t.wave_style)
        border_var.set(t.border)
        dot_var.set(t.dot)
        round_var.set(t.round)

    def start_from(label: str):
        key = bases().get(label)
        if key is None:
            return
        t = themes.resolve(key)
        name = t.name if key not in themes.BUILT_IN else "My " + t.name
        work["theme"] = themes.with_changes(t, name=name)
        name_var.set(name)
        paint()

    status = ttk.Label(card, text="", style="Muted.TLabel")

    def save(use: bool):
        name = name_var.get().strip()
        if not name:
            status.configure(text="Give it a name first.")
            return
        if name.lower() in themes.BUILT_IN:
            status.configure(text=f'"{name}" is a built-in theme: pick another name.')
            return
        try:
            size = int(size_var.get())
        except (tk.TclError, ValueError):
            size = work["theme"].font_size
        work["theme"] = themes.with_changes(work["theme"], name=name, font_size=size)
        themes.save_custom(work["theme"])
        if use:
            _save("overlay", "theme", name.lower())
            if "theme_var" in state:
                state["theme_var"].set(name.lower())
        status.configure(text=f'Saved "{name}"' + (" - the pill uses it now." if use else "."))
        base_box.configure(values=list(bases()))
        if "refresh_themes" in state:
            state["refresh_themes"]()

    def delete():
        name = name_var.get().strip()
        if name.lower() in themes.BUILT_IN or name.lower() not in themes.load_custom():
            status.configure(text="Only a theme you saved can be deleted.")
            return
        themes.delete_custom(name)
        if cfg.overlay.theme.lower() == name.lower():
            _save("overlay", "theme", "default")
        status.configure(text=f'Deleted "{name}".')
        base_box.configure(values=list(bases()))
        if "refresh_themes" in state:
            state["refresh_themes"]()

    buttons = ttk.Frame(card, style="Card.TFrame")
    buttons.configure(borderwidth=0)
    buttons.pack(fill="x", pady=(10, 0))
    ttk.Button(buttons, text="Save and use", command=lambda: save(True)).pack(side="left")
    ttk.Button(buttons, text="Save", command=lambda: save(False)).pack(side="left", padx=8)
    ttk.Button(buttons, text="Delete", command=delete).pack(side="left")
    status.pack(anchor="w", pady=(6, 0))
    paint()


def _dictation(nb, cfg: Config) -> None:
    from tkinter import ttk

    from .cleanup import Cleaner
    from .hotkey import KEY_PRESETS
    from .style import WHEEL, all_characters

    tab = _scroller(nb, "Dictation")
    card = _card(tab, "Cleanup", "Tidies each dictation before it's pasted: algorithmic rules, or "
                 "a local LLM through Ollama.")
    _check(card, "Clean up dictations", cfg.cleanup.enabled,
           lambda v: _save("cleanup", "enabled", v))
    cleaner = Cleaner(cfg.cleanup)
    models = [name for name, _ in sorted(cleaner.list_models(), key=lambda m: m[1])]
    rules = "Algorithmic (no LLM, instant)"
    row = _row(card, "Clean up with")
    current = rules if cfg.cleanup.method == "rules" else cfg.cleanup.model

    def pick_model(v: str):
        if v == rules:
            _save("cleanup", "method", "rules")
        else:
            _save("cleanup", "model", v)
            _save("cleanup", "method", "llm")

    _choice(row, [rules, *models], current, pick_model)
    if not models:
        ttk.Label(card, text="Ollama isn't answering, so only algorithmic cleanup is listed.",
                  style="Muted.TLabel").pack(anchor="w")
    row = _row(card, "Ollama")
    ollama = ttk.Label(row, text="checking...", style="Card.TLabel")
    ollama.pack(side="left")

    def restart():
        ollama.configure(text="restarting... (up to a minute)")

        def go():
            from .health import LABELS, OllamaHealth

            h = OllamaHealth(Cleaner(cfg.cleanup))
            h.restart()
            ollama.after(0, lambda: ollama.configure(text=LABELS[h.status]))

        threading.Thread(target=go, daemon=True).start()

    ttk.Button(row, text="Restart Ollama", command=restart, style="Small.TButton").pack(
        side="left", padx=10)

    def check():
        from .health import LABELS, OllamaHealth

        st = OllamaHealth(Cleaner(cfg.cleanup)).probe(full=False)
        ollama.after(0, lambda: ollama.configure(text=LABELS[st]))

    threading.Thread(target=check, daemon=True).start()

    card = _card(tab, "Style", "Rewrites the text in a character's voice and/or a tone "
                 "(one extra LLM pass).")
    row = _row(card, "Character")
    chars = ["none", *all_characters(cfg.style)]
    _choice(row, chars, cfg.style.character.lower(), lambda v: _save("style", "character", v))
    row = _row(card, "Tone")
    tones = ["neutral"] + [lv.name for levels in WHEEL.values() for lv in levels]
    _choice(row, tones, cfg.style.tone.lower(), lambda v: _save("style", "tone", v))
    row = _row(card, "Style model")
    same = f"Same as cleanup ({cfg.cleanup.model})"
    _choice(row, [same, *models], cfg.style.model or same,
            lambda v: _save("style", "model", "" if v == same else v))

    card = _card(tab, "Typing")
    _check(card, "Paste where I started (the window you were in when the dictation began)",
           cfg.inject.paste_to == "start",
           lambda v: _save("inject", "paste_to", "start" if v else "focus"))
    ttk.Label(card, text="Dictation keys (tap to start and stop; with Shift for raw text)",
              style="Card.TLabel").pack(anchor="w", pady=(8, 2))
    as_list = lambda v: [v] if isinstance(v, str) and v else list(v or [])  # noqa: E731
    keys = {"dictate": as_list(cfg.hotkeys.dictate), "raw": as_list(cfg.hotkeys.dictate_raw)}

    def toggle(name: str, on: bool):
        spec, raw_spec = KEY_PRESETS[name]
        if on and spec not in keys["dictate"]:
            keys["dictate"].append(spec)
            if raw_spec not in keys["raw"]:
                keys["raw"].append(raw_spec)
        elif not on:
            keys["dictate"] = [s for s in keys["dictate"] if s != spec]
            keys["raw"] = [s for s in keys["raw"] if s != raw_spec]
        _save("hotkeys", "dictate", keys["dictate"])
        _save("hotkeys", "dictate_raw", keys["raw"])

    for name, (spec, _raw) in KEY_PRESETS.items():
        _check(card, name, spec in keys["dictate"], lambda v, name=name: toggle(name, v))
    row = _row(card, "Mic boost")
    boosts = {"None": "none", "Low (+6 dB)": "low", "Medium (+12 dB)": "medium",
              "High (+18 dB)": "high", "Auto (lifts quiet speech)": "auto"}
    now = next((k for k, v in boosts.items() if v == cfg.audio.boost), "None")
    _choice(row, list(boosts), now, lambda v: _save("audio", "boost", boosts[v]))


def _listening(nb, cfg: Config) -> None:
    import tkinter as tk
    from tkinter import ttk

    tab = _scroller(nb, "Listening")
    card = _card(tab, "Default mode", "Where LocalFlow waits: for the start phrase, or in "
                 'command mode. Also by voice: "change default mode".')
    var = tk.StringVar(value=cfg.always_on.default_mode)
    for value, text in (("dictation", "Dictation: wait for the start phrase"),
                        ("command", "Command: always in command mode")):
        ttk.Radiobutton(card, text=text, value=value, variable=var,
                        command=lambda: _save("always_on", "default_mode", var.get())
                        ).pack(anchor="w", pady=2)

    card = _card(tab, "Timeouts", 'Also by voice: "command timeout two minutes", '
                 '"dictation timeout never".')
    for which, key, label in (("command", "command_mode_seconds", "Command mode ends after"),
                              ("dictation", "idle_stop_seconds", "Dictation ends after")):
        row = _row(card, label)
        options = {_say_seconds(v) + ("" if v == 0 else " of quiet"): v for v in TIMEOUTS[which]}
        cur = getattr(cfg.always_on, key)
        now = next((k for k, v in options.items() if v == cur), None)
        if now is None:
            now = _say_seconds(cur) + " of quiet"
            options[now] = cur
        _choice(row, list(options), now,
                lambda v, key=key, options=options: _save("always_on", key, float(options[v])),
                width=22)

    from .commands import CANCEL, EXIT

    card = _card(tab, "Escape words", "Say one of these to get out. Add your own, separated by "
                 'commas. In command mode, "show commands" lists everything you can say.')
    for key, label, built_in in (
            ("cancel_words", "Throw a dictation away (say it after the stop phrase; if the "
             "text is already in, it's deleted)", CANCEL),
            ("exit_words", "Leave command mode (your stop phrase works too)", EXIT)):
        ttk.Label(card, text=label, style="Card.TLabel", wraplength=560,
                  justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Label(card, text="Built in:  " + ",  ".join(built_in), style="Muted.TLabel",
                  wraplength=560, justify="left").pack(anchor="w")
        row = _row(card, "Your own", 12)
        var = tk.StringVar(value=", ".join(getattr(cfg.always_on, key)))
        entry = ttk.Entry(row, textvariable=var, width=46)
        entry.pack(side="left")

        def save_words(_e=None, key=key, var=var):
            words = [w.strip() for w in var.get().split(",") if w.strip()]
            _save("always_on", key, words)

        entry.bind("<FocusOut>", save_words)
        entry.bind("<Return>", save_words)
        ttk.Button(row, text="Save", style="Small.TButton",
                   command=save_words).pack(side="left", padx=8)

    card = _card(tab, "Quick mode", "For talking fast: one breath does it all. Say your start "
                 'phrase, the words, then an end word: "... continue over" pastes "continue", '
                 '"... continue send" pastes it and presses Enter. No waiting for the start '
                 "sound. An end word only counts as the last thing you say before a pause, so "
                 '"move it over here" doesn\'t stop anything. Also in the tray menu.')
    _check(card, "Quick mode on", cfg.always_on.quick, lambda v: _save("always_on", "quick", v))
    for key, label in (("quick_stop", "End words (stop)"), ("quick_send", "Send words (+ Enter)")):
        row = _row(card, label, 20)
        var = tk.StringVar(value=", ".join(getattr(cfg.always_on, key)))
        entry = ttk.Entry(row, textvariable=var, width=30)
        entry.pack(side="left")

        def save_quick(_e=None, key=key, var=var):
            words = [w.strip() for w in var.get().split(",") if w.strip()]
            if words:
                _save("always_on", key, words)

        entry.bind("<FocusOut>", save_quick)
        entry.bind("<Return>", save_quick)
        ttk.Button(row, text="Save", style="Small.TButton",
                   command=save_quick).pack(side="left", padx=8)
    ttk.Label(card, text='Code words (quick mode only): a word of your own for a command, said '
              'on its own after the start phrase or in command mode. One per line, '
              '"word = command": go = submit, nope = scratch, chrome = switch chrome. '
              "Short real words work better than single letters, and filler (ok, so, "
              "please) can't be one.", style="Muted.TLabel", wraplength=560,
              justify="left").pack(anchor="w", pady=(8, 2))
    codes = tk.Text(card, height=5, width=60, bg=FIELD, fg=TEXT, insertbackground=TEXT,
                    relief="flat", font=("Segoe UI", 10))
    codes.insert("1.0", "\n".join(cfg.always_on.code_words))
    codes.pack(anchor="w")
    codes_note = ttk.Label(card, text="", style="Muted.TLabel")
    codes_note.pack(anchor="w")

    def save_codes():
        from .commands import Grammar

        lines = [x.strip() for x in codes.get("1.0", "end").splitlines() if x.strip()]
        bad = Grammar().set_code_words(lines)
        _save("always_on", "code_words", [x for x in lines if x not in bad])
        codes_note.configure(text=("Not used (want word = command, no filler): " + "; ".join(bad))
                             if bad else f"Saved {len(lines)} code words.")

    ttk.Button(card, text="Save code words", style="Small.TButton",
               command=save_codes).pack(anchor="w", pady=(4, 0))

    card = _card(tab, "Command mode")
    _check(card, "Let the LLM work out requests the built-in commands don't cover",
           cfg.always_on.command_llm, lambda v: _save("always_on", "command_llm", v))
    _check(card, "Save dictations to history (this PC only)", cfg.history.enabled,
           lambda v: _save("history", "enabled", v))

    card = _card(tab, "Learned phrases", 'Your own ways of saying commands, learned when you '
                 'said "remember that". Select one and Forget it to drop it.')
    from .commands import BY_NAME, LEARNED, Grammar, Parsed, describe

    lst = tk.Listbox(card, height=8, bg=FIELD, fg=TEXT, selectbackground=GOLD,
                     selectforeground=BLACK, highlightthickness=0, bd=0, font=("Segoe UI", 10),
                     activestyle="none")
    lst.pack(fill="x", pady=(4, 6))
    keys: list[str] = []

    def what(d: dict) -> str:
        steps = d["steps"] if "steps" in d else [d]
        return "; ".join(describe(Parsed(BY_NAME[x["command"]], x.get("arg", ""), 1.0,
                                         where=x.get("where", ""))) for x in steps)

    def fill():
        g = Grammar(learned_file=LEARNED)
        lst.delete(0, "end")
        keys.clear()
        for phrase, d in sorted(g.learned.items()):
            keys.append(phrase)
            lst.insert("end", f'  "{phrase}"  =  {what(d)}')
        if not keys:
            lst.insert("end", '  None yet: say "remember that" when the pill offers it.')

    def forget():
        sel = lst.curselection()
        if sel and keys:
            Grammar(learned_file=LEARNED).forget(keys[sel[0]])
            fill()

    ttk.Button(card, text="Forget", command=forget, style="Small.TButton").pack(anchor="w")
    fill()


def _sounds(nb, cfg: Config) -> None:
    import tkinter as tk
    from tkinter import ttk

    from .sounds import CHOICES, play, render

    tab = _scroller(nb, "Sounds")
    card = _card(tab, "Sounds", "The response sound means a command is waiting for your "
                 'answer ("goodnight", "yes", "remember that").')
    _check(card, "Play sounds", cfg.sounds.enabled, lambda v: _save("sounds", "enabled", v))
    vol = tk.DoubleVar(value=cfg.sounds.volume)
    for key, label in (("start", "Start"), ("stop", "Stop"), ("paste", "Paste"),
                       ("response", "Response")):
        row = _row(card, label)
        cur = getattr(cfg.sounds, key)
        values = [c.capitalize() for c in CHOICES]
        if cur.lower() not in CHOICES:
            values.append(cur)
        var, _ = _choice(row, values, cur.capitalize() if cur.lower() in CHOICES else cur,
                         lambda v, key=key: (_save("sounds", key, v.lower() if v.lower() in CHOICES
                                                   else v),
                                             play(render(v.lower(), vol.get()))), width=22)
        ttk.Button(row, text="Play", style="Small.TButton",
                   command=lambda var=var: play(render(var.get().lower(), vol.get()))
                   ).pack(side="left", padx=8)
    row = _row(card, "Volume")
    scale = ttk.Scale(row, from_=0.0, to=1.0, variable=vol, length=240)
    scale.pack(side="left")
    scale.bind("<ButtonRelease-1>", lambda _e: _save("sounds", "volume", round(vol.get(), 2)))


def main(cfg: Config) -> int:
    import tkinter as tk
    from tkinter import ttk

    from .icon import set_app_identity, window_setup

    set_app_identity()
    root = tk.Tk()
    window_setup(root, "Settings")
    root.configure(bg=BLACK)
    root.geometry("760x640")
    root.minsize(620, 420)
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72)
    except tk.TclError:
        pass
    _style(root)
    head = ttk.Frame(root, padding=(18, 14, 18, 4))
    head.pack(fill="x")
    ttk.Label(head, text="LocalFlow", style="Title.TLabel").pack(side="left")
    ttk.Label(head, text="   Settings  ·  changes apply at once", style="Sub.TLabel").pack(
        side="left", pady=(8, 0))
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=8)
    state: dict = {}
    for build in (_appearance, _builder):
        build(nb, cfg, state)
    for build in (_dictation, _listening, _sounds):
        try:
            build(nb, cfg)
        except Exception:
            log.exception("settings tab failed")
    root.bind("<Escape>", lambda _e: root.destroy())
    root.after(50, lambda: (root.lift(), root.focus_force()))
    log.info("settings window open (%s)", CONFIG_PATH.name)
    root.mainloop()
    return 0
