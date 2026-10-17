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

print("\nall passed" if not failures else f"\n{failures} FAILED")
