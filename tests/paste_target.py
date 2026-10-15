"""Paste where I started: text and "submit" go to the window the dictation began in.

Opens two small windows, A and B, in a separate process (Windows' foreground
lock only applies across processes), then: starts "dictating" in A, moves to
B, and delivers through the real pipeline. A must get the text and the Enter,
B must be untouched, focus must come back to B. Then the same with A closed:
the text must go to the clipboard, never into B. Takes a few seconds and
briefly shows the two windows.

    uv run python tests/paste_target.py
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow import window  # noqa: E402
from localflow.always_on import COMMANDS  # noqa: E402
from localflow.config import Config  # noqa: E402
from localflow.engine import Engine, Result  # noqa: E402
from localflow.inject import get_clipboard_text  # noqa: E402

HELPER = r'''
import json, sys, tkinter as tk
log, out = [], sys.argv[1]
root = tk.Tk(); root.withdraw()
def make(name, x):
    w = tk.Toplevel(root); w.title(f"LocalFlow test {name}"); w.geometry(f"360x70+{x}+300")
    e = tk.Entry(w, width=40); e.pack(pady=18)
    e.bind("<Return>", lambda ev: log.append([name, e.get()]))
    w.bind("<FocusIn>", lambda ev: e.focus_set())  # as if clicked into the box
    return w, e
wa, ea = make("A", 150); wb, eb = make("B", 560)
def dump():
    json.dump({"enter": log, "B": eb.get()}, open(out, "w")); root.after(150, dump)
root.after(150, dump); root.after(20000, root.destroy); root.mainloop()
'''


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="localflow-paste-"))
    (tmp / "helper.py").write_text(HELPER, encoding="utf-8")
    state_file = tmp / "state.json"
    helper = subprocess.Popen([sys.executable, str(tmp / "helper.py"), str(state_file)])
    user32 = ctypes.windll.user32
    failures = 0

    def expect(name: str, ok: bool, detail: str) -> None:
        nonlocal failures
        failures += not ok
        print(f"  {'pass' if ok else 'FAIL'}  {name}: {detail}")

    try:
        time.sleep(2.0)
        a = user32.FindWindowW(None, "LocalFlow test A")
        b = user32.FindWindowW(None, "LocalFlow test B")
        state = lambda: json.loads(state_file.read_text())  # noqa: E731
        focus = lambda h: window.activate(window.Target(h, None, ""))  # noqa: E731

        cfg = Config()
        cfg.inject.paste_to = "start"
        engine = Engine(cfg)
        say = lambda text: setattr(engine, "process", lambda audio, **kw: Result(  # noqa: E731
            text=text, raw=text, audio_seconds=1, transcribe_seconds=0, cleanup_seconds=0))
        silence = np.zeros(16000, np.float32)

        print("text + submit while you're in another window")
        focus(a); time.sleep(0.3); target = window.current()
        focus(b); time.sleep(0.3)
        say("book the meeting room")
        engine._process_and_inject(silence, True, True, "test", None,
                                   lambda t: COMMANDS["submit"](), target=target)
        time.sleep(0.8)
        s = state()
        expect("A got the text and Enter", s["enter"] == [["A", "book the meeting room"]], str(s["enter"]))
        expect("B untouched", s["B"] == "", repr(s["B"]))
        expect("back in B", user32.GetForegroundWindow() == b, "")

        print("start window closed before the text is ready")
        focus(a); time.sleep(0.3); target = window.current()
        user32.PostMessageW(a, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(0.6); focus(b); time.sleep(0.3)
        say("second message")
        engine._process_and_inject(silence, True, True, "test", None, None, target=target)
        time.sleep(0.5)
        expect("nothing typed into B", state()["B"] == "", repr(state()["B"]))
        expect("text on the clipboard", get_clipboard_text() == "second message", "")
        expect("shown as an error", engine.state.value == "error", engine.state.value)
    finally:
        helper.terminate()

    print("\nall passed" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
