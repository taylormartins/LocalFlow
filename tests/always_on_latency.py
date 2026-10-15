"""How quickly always-on reacts: seconds from the end of a phrase to its detection.

Plays a TTS conversation into the listener in real time (0.1 s blocks, with
the real threads), so the burst hang-over, the detector's queue and the model
time all count, as they do live. Enrols from TTS in a scratch folder, like
tests/always_on.py.

    uv run python tests/always_on_latency.py          # this PC's devices
    uv run python tests/always_on_latency.py --cpu    # as on a PC without a GPU
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import always_on as T  # noqa: E402  (the TTS helpers)

from localflow import wake  # noqa: E402
from localflow.config import Config, load  # noqa: E402


def main() -> int:
    cpu = "--cpu" in sys.argv
    wake.WAKE_DIR = T.TMP / "wake"
    cfg = Config() if cpu else load()[0]
    cfg.history.enabled = False  # never into your own history
    if cpu:
        cfg.whisper.model, cfg.whisper.device, cfg.whisper.compute_type = "small.en", "cpu", "int8"
    cfg.cleanup.method = "rules"
    cfg.audio.boost = "none"

    det = wake.Detector(cfg.always_on.model, wake.Phrases())
    phrases = wake.Phrases()
    for which, text in (("start", T.START), ("stop", T.STOP)):
        takes = [wake.trim(T.say(text, r)) for r in (0, -1, 1)]
        heard = [" ".join(w.text for w in det.words(t)) for t in takes]
        setattr(phrases, which, wake.make_phrase(which, text, [], takes, heard))
    phrases.save()

    from localflow.always_on import Listener
    from localflow.engine import Engine

    engine = Engine(cfg)
    engine.transcriber.load()
    marks: dict[str, float] = {}
    clock = {"t0": 0.0, "fed": 0}

    def event(kind: str, text: str) -> None:
        marks.setdefault(kind, time.monotonic())
        if "-v" in sys.argv:
            # How far the listener's audio is behind what has been fed.
            lag = (clock["fed"] - lis.ring.end) / 16000
            print(f"  [{kind}] {text}   (audio thread {lag:.2f} s behind)")

    lis = Listener(engine, cfg, inject=False, on_event=event)
    lis.start(stream=False)
    lis.detector.warm()  # as the app does at start (in the background there)
    if "--probe" in sys.argv:
        for i in range(3):
            t = time.perf_counter()
            lis.detector.words(wake.trim(T.say(T.START)))
            print(f"  probe pass {i}: {time.perf_counter() - t:.2f}s")
    print(f"listening model on: {getattr(lis.detector, 'device', 'cpu')}; dictation model on: "
          f"{engine.transcriber.device_in_use}")

    pieces = [
        ("lead", T.build(1.0)),
        ("start", wake.trim(T.say(T.START), pad=0.0)),
        ("body", T.build(0.4, T.say("Please send the report to the whole team today."), 0.4)),
        ("stop", wake.trim(T.say(T.STOP), pad=0.0)),
        ("tail", T.build(3.0)),
    ]
    ends: dict[str, float] = {}
    t0 = time.monotonic()
    fed = 0
    for name, audio in pieces:
        for i in range(0, len(audio), 1600):
            lis.feed_realtime(audio[i : i + 1600])
            fed += min(1600, len(audio) - i)
            clock["fed"] = fed
            # Keep the clock honest: feed no faster than real time.
            ahead = t0 + fed / 16000 - time.monotonic()
            if ahead > 0:
                time.sleep(ahead)
        ends[name] = time.monotonic()
    time.sleep(2.0)
    lis.stop()

    for k in ("start", "stop"):
        if k in marks:
            print(f"{k:5} phrase: detected {marks[k] - ends[k]:.2f} s after it was said")
        else:
            print(f"{k:5} phrase: NOT detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
