"""'Cancel' after the stop phrase must stop the work at once, not wait for the
slow cleanup model and then throw the result away. A fake transcriber and a
fake 6 s cleaner; no models. Control: without cancel it takes the full 6 s.

    uv run python tests/cancel_midway.py
"""
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow.config import Config
from localflow.engine import Engine

failures = 0


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


class SlowCleaner:
    uses_llm = True
    last_outcome = ""
    calls = 0

    def clean(self, raw):
        SlowCleaner.calls += 1
        time.sleep(6)
        return "CLEANED " + raw


cfg = Config()
cfg.history.enabled = False
cfg.cleanup.enabled = True
engine = Engine(cfg)
engine.transcriber = type("T", (), {"transcribe": lambda self, a: "hello there world"})()
engine.cleaner = SlowCleaner()
audio = np.zeros(16000 * 2, dtype=np.float32)


def run(cancel_after):
    engine._discard = False
    out = {}
    t0 = time.perf_counter()
    th = threading.Thread(target=lambda: out.update(
        r=engine._process_and_inject(audio, True, False)), daemon=True)
    th.start()
    if cancel_after is not None:
        time.sleep(cancel_after)
        expect("discard() reports a dictation on its way", engine.discard())
    th.join(10)
    return time.perf_counter() - t0, th.is_alive()


print("control: no cancel")
took, alive = run(None)
expect("waits for the 6 s cleanup", took >= 5.5 and not alive, f"{took:.1f}s")

print("cancel while cleaning")
took, alive = run(0.5)
expect("finishes within a second of the cancel", took < 1.5 and not alive, f"{took:.1f}s")
expect("nothing pasted, state back to idle", engine.state.name == "IDLE", engine.state.name)
expect("discard flag reset for the next dictation", not engine._discard)

print("cancel before it starts cleaning (during transcribe)")
engine._discard = True
res = engine.process(audio)
expect("skips the cleanup model entirely", res.text == "hello there world", res.text)

sys.exit(1 if failures else 0)
