"""Settings reach the running app: a change saved to config.toml (as the
Settings window saves it) is applied live by the tray's watcher, with what
each change needs (a model switch, rebinding keys). Custom themes save, load
and delete. Uses scratch files, never your config.toml or themes.json.

    uv run python tests/settings_apply.py
"""
import copy
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow import commands, config, themes
from localflow.config import Config, save_setting
from localflow.engine import Engine
from localflow.tray import Tray

failures = 0


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


tmp = Path(tempfile.mkdtemp())
config.CONFIG_PATH = tmp / "config.toml"
config.CONFIG_PATH.write_text("[sounds]\nstart = \"tick\"\n", encoding="utf-8")
commands.LEARNED = tmp / "learned.json"
themes.STORE = tmp / "themes.json"

cfg = Config()
cfg.history.enabled = False
engine = Engine(cfg)
calls = []
engine.cleaner.switch_model = lambda name, previous=None: calls.append(("switch", name, previous))
engine.cleaner.unload = lambda: calls.append(("unload",))
tray = Tray(engine, on_keys_changed=lambda: calls.append(("keys",)))

print("apply_settings")
new = copy.deepcopy(cfg)
new.sounds.start = "pop"
new.overlay.theme = "matrix"
new.whisper.model = "tiny.en"  # needs a restart: not applied live
changed = tray.apply_settings(new)
expect("live settings copied", cfg.sounds.start == "pop" and cfg.overlay.theme == "matrix",
       str(changed))
expect("a restart-only setting is left alone", cfg.whisper.model != "tiny.en")
new = copy.deepcopy(cfg)
new.cleanup.method = "rules"
tray.apply_settings(new)
time.sleep(0.3)
expect("algorithmic cleanup unloads the LLM", ("unload",) in calls, str(calls))
new = copy.deepcopy(cfg)
new.cleanup.method, new.cleanup.model = "llm", "gemma4:12B"
previous = cfg.cleanup.model
tray.apply_settings(new)
time.sleep(0.3)
expect("a new cleanup model is switched to", ("switch", "gemma4:12B", previous) in calls, str(calls))
new = copy.deepcopy(cfg)
new.hotkeys.dictate = ["<f9>"]
tray.apply_settings(new)
expect("new dictation keys are bound", ("keys",) in calls, str(calls))
expect("nothing changed: nothing done", tray.apply_settings(copy.deepcopy(cfg)) == {})

print("the watcher")
tray.watch_settings()
time.sleep(1.2)
save_setting("sounds", "response", "chime up")
save_setting("always_on", "command_mode_seconds", 0.0)
deadline = time.monotonic() + 4
while time.monotonic() < deadline and cfg.sounds.response != "chime up":
    time.sleep(0.1)
expect("a saved change is applied within a second or two",
       cfg.sounds.response == "chime up" and cfg.always_on.command_mode_seconds == 0,
       f"{cfg.sounds.response} {cfg.always_on.command_mode_seconds}")

print("themes")
mine = themes.with_changes(themes.ROYAL, name="Midnight", bg="#000022", wave_style="dots")
themes.save_custom(mine)
got = themes.resolve("midnight")
expect("a saved theme loads by name", got.bg == "#000022" and got.wave_style == "dots", str(got))
expect("it's listed after the built-in ones", list(themes.all_themes())[-1] == "midnight")
themes.save_custom(themes.with_changes(themes.DEFAULT, name="Royal", bg="#ff0000"))
expect("a custom theme can't replace a built-in", themes.resolve("royal").bg == themes.ROYAL.bg)
themes.delete_custom("Midnight")
expect("deleted", themes.resolve("midnight") is themes.DEFAULT)
expect("an unknown name falls back to the default", themes.resolve("nope") is themes.DEFAULT)
expect("blend", themes.blend("#000000", "#ffffff", 0.5) == "#808080",
       themes.blend("#000000", "#ffffff", 0.5))

print("escape words and the command sheet")
from localflow.commands import COMMANDS, HELP_SHEET, Grammar, help_sections  # noqa: E402

on_sheet = {n for _t, rows in HELP_SHEET for _s, _d, names in rows for n in names}
missing = [c.name for c in COMMANDS if c.name not in on_sheet]
expect("every command is on the command sheet", not missing, str(missing))
g = Grammar()
expect("a built-in escape word", g.is_one(["never", "mind"], g.cancel))
expect("...not yet one of your own", not g.is_one(["abort", "mission"], g.cancel))
new = copy.deepcopy(cfg)
new.always_on.cancel_words = ["abort mission"]
new.always_on.exit_words = ["over and out"]


class FakeListener:
    grammar = g

    def _extend_mode(self):
        pass


tray.always_on = FakeListener()
tray.apply_settings(new)
expect("your own escape words apply live", g.is_one(["abort", "mission"], g.cancel)
       and g.is_one(["over", "and", "out"], g.exit) and g.is_one(["never", "mind"], g.cancel))
sheet = dict(help_sections())
expect("...and show on the command sheet",
       any("abort mission" in say for say, _ in sheet["After the stop phrase"])
       and any("over and out" in say for say, _ in sheet["Command mode"]))
tray.always_on = None

print("every theme draws")
import tkinter as tk  # noqa: E402

root = tk.Tk()
root.withdraw()
canvas = tk.Canvas(root, width=300, height=120)
errors = []
styles = [themes.with_changes(themes.CLASSIC, wave_style=w, border=b, dot=d, edge="#888888")
          for w in themes.WAVE_STYLES for b in themes.BORDERS for d in themes.DOT_STYLES]
for theme in list(themes.BUILT_IN.values()) + styles:
    for t in (0.0, 0.4, 1.3):
        try:
            canvas.delete("all")
            themes.pill(canvas, theme, 0, 0, 250, 42, 21, 1.5, t)
            themes.recording_dot(canvas, theme, 22, 21, 1.5, themes.dot_colour(theme, "llm"), t)
            themes.wave(canvas, theme, themes.preview_levels(t), 38, 192, 21, 26, 1.5, 0.009, t)
            for box in ((2, 2, 30, 27), (0, 0, 200, 120)):  # a click label, a screen number
                themes.label_box(canvas, theme, *box, 1.5, "12", themes.font(theme, 1.5, True))
        except Exception as exc:
            errors.append(f"{theme.name}/{theme.wave_style}/{theme.border}/{theme.dot}: {exc}")
root.destroy()
expect(f"{len(styles) + len(themes.BUILT_IN)} themes and combinations draw without an error",
       not errors, "; ".join(errors[:3]))
expect("click labels: 'match' follows the pill, 'yellow' is the original, or any theme",
       themes.label_theme("match", themes.ICE) is themes.ICE
       and themes.label_theme("yellow", themes.ICE) is themes.CLASSIC_LABELS
       and themes.label_theme("classic", themes.ICE) is themes.CLASSIC
       and not set(themes.LABEL_CHOICES) & set(themes.BUILT_IN)
       and themes.label_theme("fire", themes.ICE) is themes.FIRE)
from localflow import overlay  # noqa: E402

fake = overlay.Overlay.__new__(overlay.Overlay)
fake.cfg, fake._themes = cfg.overlay, themes.Current()
_, label_font = fake._label_look()
expect("label numbers come out the size asked for (Tk's negative pixels once made them all 8)",
       abs(label_font(-20)[1]) == 20 and abs(label_font(-60)[1]) == 60,
       f"{label_font(-20)} {label_font(-60)}")
expect("label size has a 10 pt floor", themes.LABEL_MIN >= 13)
expect("Royal is the default",
       themes.resolve("") is themes.ROYAL and themes.resolve("default") is themes.ROYAL)
expect("the gem keeps the dot's three meanings apart",
       len({themes.dot_colour(themes.ROYAL, k) for k in ("llm", "rules", "raw")}) == 3)
expect("black and white means black and white",
       themes.colour_for(themes.NEWSPAPER, "#ef5350", "#ef5350") == themes.NEWSPAPER.accent)

print("\nall passed" if not failures else f"\n{failures} FAILED")
