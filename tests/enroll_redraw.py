"""Phrase setup: when the last take's check finishes inside the redraw
throttle, the window still redraws (it used to say "checking..." until
clicked). No window, no microphone, no model.

    uv run python tests/enroll_redraw.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from localflow import enroll  # noqa: E402


class Root:
    def after(self, ms, fn):
        pass


class Take:
    checking = True


cls = next(v for v in vars(enroll).values() if isinstance(v, type) and hasattr(v, "_tick"))
setup = cls.__new__(cls)
take = Take()
setup.takes = {"start": [take], "stop": []}
setup.state = "idle"
setup.root = Root()
drawn = []
setup._refresh = lambda redraw=True: drawn.append(time.monotonic())
setup._tick()  # checking: draws
take.checking = False  # the check finishes straight after that draw
setup._tick()  # inside the 0.25 s throttle
setup._tick()
ok = len(drawn) == 2
print(f"  {'pass' if ok else 'FAIL'}  the finished check is drawn ({len(drawn)} redraws, want 2)")
print("\nall passed" if ok else "\n1 FAILED")
sys.exit(0 if ok else 1)
