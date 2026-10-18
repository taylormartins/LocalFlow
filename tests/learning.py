"""Command mode, through the real always-on listener: learning from missed
commands, switching to dictation and back, and the grid's numbers.

Words are fed in as if heard, so no speech model is loaded: runs in a
second, on any PC. Uses a scratch learned-phrases file, never yours.

    uv run python tests/learning.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np

from localflow import commands as commands_mod
from localflow import sounds as sounds_mod
from localflow.always_on import Listener
from localflow.config import Config
from localflow.engine import Engine
from localflow.wake import Word

tmp = Path(tempfile.mkdtemp())
commands_mod.LEARNED = tmp / "learned.json"
cfg = Config()
cfg.history.enabled = False
cfg.cleanup.method = "rules"  # no LLM: misses stay misses
engine = Engine(cfg)
events = []
lis = Listener(engine, cfg, inject=False, on_event=lambda k, t: events.append((k, t)))
real_do = lis._do


def do(p, after_dictation=False, confirmed=False, ok=True):
    real_do(p, after_dictation=after_dictation, confirmed=confirmed)
    lis._command_done(p, "ok" if ok else "no window called heroes", ok)


lis._do = do
played = []
sounds_mod.play = lambda data: played.append(data)  # quiet, and countable
failures = 0


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


def say(text):
    """One burst in command mode, as if just heard."""
    words = [Word(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(text.split())]
    say.n = getattr(say, "n", 0) + 1
    a = say.n * 10 * 16000  # each burst later than the last
    lis.ring.end = a + 16000
    lis._handle(a, a + 16000, np.zeros(16000, np.float32), words)
    if lis._held:  # an unrecognised request: finish it as the pause would
        request, lis._held = " ".join(lis._held), []
        lis._finish_request(request)


def kinds(kind):
    return [t for k, t in events if k == kind]


lis.commanding = True
print("a miss, a working command, 'remember that'")
say("roll the page downward")
say("scroll down")
say("remember that")
expect("learned", kinds("learned") == ["roll the page downward = scroll_down"], str(events))
expect("saved to the file", "roll the page downward" in commands_mod.LEARNED.read_text())
events.clear()
say("roll the page downward")
expect("works from then on", kinds("ran") == ["scroll_down"], str(events))

print("a command that fails counts as a miss (no LLM)")
events.clear()
lis._do = lambda p, after_dictation=False, confirmed=False: do(
    p, after_dictation, confirmed, ok=p.arg != "heroes")
say("select heroes")
say("scroll up")
expect("offered", kinds("learn-offer") == ["select heroes = scroll_up"], str(events))
say("okay thanks")
expect("not accepted: nothing learned", not kinds("learned"), str(events))

print("a fresh listener (restart) still knows it")
lis2 = Listener(engine, cfg, inject=False)
p = lis2.grammar.parse("roll the page downward".split())
expect("learned phrase loads from the file", p is not None and p.command.name == "scroll_down", str(p))
lis2.grammar.forget("roll the page downward")
expect("forget removes it", lis2.grammar.parse("roll the page downward".split()) is None)

print("switching modes")
import time  # noqa: E402

from localflow import commands as cmds  # noqa: E402
from localflow.engine import State  # noqa: E402

lis.commanding = True
events.clear()
say("dictation mode")
expect("'dictation mode' leaves command mode and starts a dictation",
       lis.dictating and not lis.commanding and ("start", "dictation mode") in events, str(events))
# The dictation ends (stop phrase; nothing transcribed here) and the paste is done.
lis.dictating = False
engine.cancel_external()  # what a finished dictation leaves: idle
lis._on_engine_state(State.IDLE, "")
time.sleep(0.9)
expect("...and command mode comes back after it", lis.commanding, str(events))
lis._return_to_commands = False

print("the grid's numbers")
lis.commanding = True
cmds._grid[:] = [(0, 0, 900, 900)]
say("five")
expect("a number on its own zooms into that cell", cmds._grid[:1] == [(300, 300, 600, 600)],
       str(cmds._grid))
say("nine")
expect("...and again", cmds._grid[:1] == [(500, 500, 600, 600)], str(cmds._grid))
cmds.hide_grid()

print("default mode")
from localflow import config as config_mod  # noqa: E402
from localflow.engine import Result  # noqa: E402

saved = []
config_mod.save_setting = lambda *a, **k: saved.append(a)  # never your config.toml
lis.commanding = True
events.clear()
say("change default mode")
expect("'change default mode' asks, with the response sound",
       kinds("confirm") == ["default_mode"] and played, f"{events} played={len(played)}")
say("command mode")
expect("...'command mode' makes it the default and saves it",
       cfg.always_on.default_mode == "command" and saved[-1] == ("always_on", "default_mode", "command"),
       f"{cfg.always_on.default_mode} {saved}")
lis.ring.end = lis._mode_until + 16000
lis.ingest(np.zeros(1600, np.float32))
expect("command mode no longer times out", lis.commanding)
say("default mode dictation mode")
expect("said in one go: 'default mode dictation mode'", cfg.always_on.default_mode == "dictation",
       cfg.always_on.default_mode)
lis.ring.end = lis._mode_until + 16000
lis.ingest(np.zeros(1600, np.float32))
expect("...where command mode times out again", not lis.commanding)
lis.commanding = True
played.clear()
say("change default mode")
say("banana")
expect("anything else leaves it unchanged", cfg.always_on.default_mode == "dictation" and not lis._choice)
cfg.sounds.response = "none"
played.clear()
say("change default mode")
expect("response sound 'none' is silent", all(d is None for d in played), str(played[:1]))
say("never mind")
cfg.sounds.response = "triple click"

print("sleep waits for goodnight (inject is off: nothing really sleeps)")
lis.commanding = True
played.clear()
events.clear()
say("go to sleep")
expect("'go to sleep' arms with the response sound", kinds("confirm") == ["sleep"] and played,
       f"{events} played={len(played)}")
say("goodnight")
expect("...'goodnight' runs it", kinds("ran") == ["sleep"], str(events))
events.clear()
say("go to sleep")
say("banana")
expect("anything else cancels", not kinds("ran"), str(events))

print("cancel after the stop phrase")
lis.leave_commands()
sent = []
engine.injector.send = lambda text: sent.append(text)
engine.process = lambda audio, **k: Result("hello there", "hello there", 1.0, 0.1, 0.0)
a = 900 * 16000
lis.ring.end = a + 16000
lis._cmd_from, lis._cmd_until, lis._delivered = a, a + 5 * 16000, False
engine.state = State.TRANSCRIBING
events.clear()
taken = lis._check_command(a, [Word("cancel", 0.1, 0.4)], 0.0)
expect("'cancel' while it's transcribing is taken", taken and kinds("cancelled"), str(events))
engine._process_and_inject(np.zeros(16000, np.float32), True, True, "always-on")
expect("...and nothing is pasted", not sent and engine.state == State.IDLE, str(sent))
engine._process_and_inject(np.zeros(16000, np.float32), True, True, "always-on")
expect("the next dictation pastes as normal", sent == ["hello there"], str(sent))
lis._cmd_until = a + 5 * 16000
events.clear()
lis._check_command(a, [Word("command", 2.0, 2.3), Word("mode", 2.3, 2.6)], 0.0)
expect("the window stays open: 'command mode' can follow", lis.commanding, str(events))
lis.leave_commands()
lis._cmd_from, lis._cmd_until, lis._delivered = a, a + 5 * 16000, True
events.clear()
lis._check_command(a, [Word("never", 0.1, 0.3), Word("mind", 0.3, 0.5)], 0.0)
expect("after the paste, 'never mind' deletes it (scratch)", ("ran", "scratch") in events, str(events))

print("\nall passed" if not failures else f"\n{failures} FAILED")
