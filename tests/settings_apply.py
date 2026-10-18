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
new.overlay.theme = "royal"
new.whisper.model = "tiny.en"  # needs a restart: not applied live
changed = tray.apply_settings(new)
expect("live settings copied", cfg.sounds.start == "pop" and cfg.overlay.theme == "royal",
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

print("\nall passed" if not failures else f"\n{failures} FAILED")
