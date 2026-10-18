"""The stop phrase caught from the live transcript, when the small listening
model misses it. No speech model: the live preview's text and the recording
are faked. Uses a made-up phrase, never yours.

    uv run python tests/live_stop.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow import commands
from localflow.always_on import Listener
from localflow.config import Config
from localflow.engine import Engine
from localflow.live import LivePreview
from localflow.wake import Phrase, Phrases

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
lis.finish = lambda keep=None: (finished.append(keep), setattr(lis, "dictating", False))
live = LivePreview(engine, cfg.overlay)
lis.watch_live(live)


def dictating():
    finished.clear()
    events.clear()
    lis.dictating = True
    engine.recorder.start_external()


print("the live text")
dictating()
live._set("Send Sam the budget on Friday.", 48000)
expect("ordinary words: nothing", not finished)
live._set("Send Sam the budget on Friday. That will be all.", 64000)
expect("ends with the stop phrase: the dictation finishes there", finished == [64000],
       f"{finished} {events}")
expect("...and says so", ("stop", "that will be all") in events, str(events))
expect("...and opens the command window", lis._cmd_until > lis._cmd_from, "")
engine.recorder.stop()

dictating()
live._set("That will be all the budget for Friday, Sam.", 64000)
expect("the phrase with more after it: not a stop", not finished, str(finished))
live._set("Send it, that will be all", 64000)
expect("no full stop needed", finished == [64000], str(finished))
engine.recorder.stop()

dictating()
lis.dictating = False
live._set("That will be all.", 64000)
expect("not dictating: ignored", not finished, str(finished))
engine.recorder.stop()

print("\nall passed" if not failures else f"\n{failures} FAILED")
