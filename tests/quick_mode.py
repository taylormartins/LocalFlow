"""Quick mode (one-breath end words, "send", code words) and input
buffering (a command said right after the stop phrase, or during a long
cleanup, is held until the text is in). No speech model: the words are fed
in as if heard. Uses a made-up phrase, never yours.

    uv run python tests/quick_mode.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np

from localflow import commands
from localflow import sounds as sounds_mod
from localflow.always_on import RATE, Listener
from localflow.config import Config
from localflow.engine import Engine, State
from localflow.live import LivePreview
from localflow.wake import Hit, Phrase, Phrases, Word

failures = 0


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


commands.LEARNED = Path(__file__).parent / "_unused_learned.json"
cfg = Config()
cfg.history.enabled = False
cfg.cleanup.method = "rules"
engine = Engine(cfg)
events = []
lis = Listener(engine, cfg, inject=False, on_event=lambda k, t: events.append((k, t)))
lis.detector.set_phrases(Phrases(start=Phrase(text="Okay listen, Marigold."),
                                 stop=Phrase(text="That will be all.")))
finished = []


def fake_finish(keep=None, drop=""):
    finished.append((keep, drop))
    lis.dictating = False


lis.finish = fake_finish
live = LivePreview(engine, cfg.overlay)
lis.watch_live(live)
SILENCE = np.zeros(RATE, np.float32)
played = []
sounds_mod.play = lambda data: played.append(data)  # quiet, and countable


def words(text, at=0.0):
    return [Word(w, at + i * 0.3, at + i * 0.3 + 0.25) for i, w in enumerate(text.split())]


def dictating(start):
    finished.clear()
    events.clear()
    lis.dictating = True
    lis._dict_start = start
    lis._dict_heard = True
    lis._prev_burst = None
    lis._consumed = start
    lis._pending = None
    lis._delivered = False
    engine.recorder.start_external()


def pending():
    return lis._pending.command.name if lis._pending else None


print("input buffering: the live preview catches the stop phrase late")
a = 1000 * RATE
dictating(a)
# The phrase ended 4 s in; the preview only noticed at 6 s, and "submit"
# was said at 4.5 s, between the two.
lis.ring.end = a + 6 * RATE
live._set("Send Sam the budget. That will be all.", 4 * RATE)
engine.recorder.stop()
expect("the preview's stop finishes the dictation", finished == [(4 * RATE, "")], str(finished))
played.clear()
lis._handle(a + int(4.5 * RATE), a + int(5.2 * RATE), SILENCE, words("submit"))
expect("'submit' said straight after the phrase is held for the paste", pending() == "submit",
       f"{events}")
expect("...and clicks right away, before the paste",
       sounds_mod.render(cfg.sounds.command, cfg.sounds.volume) in played, f"played={len(played)}")

print("input buffering: a command during a long cleanup")
a = 2000 * RATE
dictating(a)
lis.ring.end = a + 10 * RATE
lis._end(a, Hit("stop", 1.0, 8.0, 9.0, "that will be all", "text"))
engine.recorder.stop()
later = a + 30 * RATE  # cleanup still running 21 s after the stop phrase
lis.ring.end = later + RATE
lis._handle(later, later + RATE, SILENCE, words("submit"))
expect("'submit' 21 s later, before the text is in, is still held", pending() == "submit",
       str(events))
events.clear()
lis._delivered_now("Send Sam the budget.")
expect("...and runs once the text is in", ("ran", "submit") in events, str(events))
expect("...and that was this dictation's one command", lis._cmd_until == 0)
a = 2500 * RATE
dictating(a)
lis.ring.end = a + 10 * RATE
lis._end(a, Hit("stop", 1.0, 8.0, 9.0, "that will be all", "text"))
engine.recorder.stop()
lis.ring.end = a + 40 * RATE
lis._delivered_now("Send Sam the budget.")
expect("no command yet: once the text is in, command_window more seconds",
       lis._cmd_until == lis.ring.end + int(cfg.always_on.command_window * RATE))

a = 3000 * RATE
dictating(a)
lis.ring.end = a + 10 * RATE
lis._end(a, Hit("stop", 1.0, 8.0, 9.0, "that will be all", "text"))
engine.recorder.stop()
lis._on_engine_state(State.IDLE, "nothing said")
expect("nothing delivered (nothing said): the buffer closes after command_window",
       lis._cmd_until == lis.ring.end + int(cfg.always_on.command_window * RATE))

print("quick mode")
cfg.always_on.quick = True
a = 4000 * RATE
dictating(a)
lis.ring.end = a + 3 * RATE
lis._last_speech = a + 2 * RATE
lis._handle(a, a + int(2.2 * RATE), SILENCE, words("keep going over"))
expect("'... over' at the end of a burst stops it", finished and finished[0][1] == "over",
       str(finished))
expect("...cut between 'going' and the end word",
       finished and int(0.55 * RATE) <= finished[0][0] <= int(0.6 * RATE), str(finished))
expect("...and no Enter", pending() is None)

dictating(a)
lis._handle(a, a + int(2.2 * RATE), SILENCE, words("continue send"))
expect("'... send' stops it and holds Enter for the paste",
       finished and finished[0][1] == "send" and pending() == "submit", f"{finished} {pending()}")
events.clear()
lis._delivered_now("Continue.")
expect("...pressed once the text is in", ("ran", "submit") in events, str(events))

dictating(a)
lis._last_speech = a + 3 * RATE  # still talking after the burst: "over... here"
lis._handle(a, a + int(2.2 * RATE), SILENCE, words("move it over"))
expect("talking straight on after 'over': not the end", not finished, str(finished))
lis._last_speech = a + 2 * RATE
lis._handle(a, a + int(2.2 * RATE), SILENCE, words("over the moon"))
expect("'over' not last: not the end", not finished, str(finished))

cfg.always_on.quick_send = ["send", "ship it"]
dictating(a)
lis._handle(a, a + int(2.2 * RATE), SILENCE, words("all done ship it"))
expect("a two-word send phrase works too", finished and finished[0][1] == "ship it"
       and pending() == "submit", f"{finished} {pending()}")
cfg.always_on.quick_send = ["send"]

cfg.always_on.quick = False
dictating(a)
lis._handle(a, a + int(2.2 * RATE), SILENCE, words("keep going over"))
expect("quick mode off: 'over' is just a word", not finished, str(finished))
lis.dictating = False
engine.recorder.stop()

print("the do word: one command and out")
ran = []
real_do = lis._do
lis._do = lambda p, after_dictation=False, confirmed=False: ran.append(p.command.name)
cfg.always_on.quick = True
lis.dictating = False
ran.clear()
took = lis._command_instead(a, words("do enter"))
expect("'<start> do enter' presses Enter", took and ran == ["submit"], str(ran))
expect("...and leaves no command mode open", not lis.commanding)
ran.clear()
lis._command_instead(a, words("do"))
expect("'<start> do' alone opens command mode for one command", lis.commanding and lis._oneshot)
lis._take(commands.Parsed(commands.BY_NAME["new_tab"], "", 1.0))
expect("...which closes after that command", ran == ["new_tab"] and not lis.commanding, str(ran))
lis._command_instead(a, words("do show labels"))
expect("'do show labels' stays in command mode, to click one", lis.commanding)
lis.leave_commands()
expect("'do the dishes tonight' is still a dictation",
       not lis._command_instead(a, words("do the dishes tonight")))
cfg.always_on.quick = False
ran.clear()
expect("works with quick mode off too", lis._command_instead(a, words("do enter"))
       and ran == ["submit"] and not lis.commanding, str(ran))
for heard in ("to show commands", "undo show commands"):
    ran.clear()
    took = lis._command_instead(a, words(heard))
    expect(f"misheard: '{heard}' shows the commands", took and ran == ["help"], str(ran))
    lis.leave_commands()
ran.clear()
lis._command_instead(a, words("undo"))
expect("'undo' on its own is still undo", ran == ["undo"], str(ran))
lis.leave_commands()
expect("'to the shops' is still a dictation", not lis._command_instead(a, words("to the shops")))
lis._do = real_do

print("the start phrase in the window after a dictation")
events.clear()
lis._cmd_from, lis._cmd_until, lis._delivered = a, a + 5 * RATE, True
took = lis._check_command(a, words("okay listen marigold do show commands"), 0.0)
expect("isn't a missed command (it was once offered to be learned as 'scratch')",
       not took and not [e for e in events if e[0] == "unknown"], str(events))

print("the end word comes off the text if the cut left it")
caught = []
real = Listener(engine, cfg, inject=False)
real.dictating = True
engine.finish_external = lambda keep, transform=None, inject=True, done=None: caught.append(transform)
real.finish(keep=100, drop="over")
t = caught[-1]
expect("'Continue over.' -> 'Continue'", t("Continue over.") == "Continue", t("Continue over."))
expect("'Move it over here.' is left alone", t("Move it over here.") == "Move it over here.")
real.dictating = True
real.finish(keep=100, drop="ship it")
expect("'All done, ship it.' -> 'All done'", caught[-1]("All done, ship it.") == "All done",
       caught[-1]("All done, ship it."))

print("code words")
lis.grammar.set_code_words(["go = submit", "chrome = switch chrome"])
p = lis.grammar.parse(["go"])
expect("'go' = submit", p is not None and p.command.name == "submit", str(p))
p = lis.grammar.parse(["chrome"])
expect("'chrome' = switch to chrome", p is not None and p.command.name == "switch"
       and p.arg == "chrome", str(p))
lis.grammar.set_code_words([])
expect("off with quick mode", lis.grammar.parse(["go"]) is None)
bad = lis.grammar.set_code_words(["ok = submit", "zap = nothing_real", "go submit"])
expect("filler, unknown commands and lines without '=' are refused", len(bad) == 3, str(bad))
lis.grammar.set_code_words([])

print("\nall passed" if not failures else f"\n{failures} FAILED")
