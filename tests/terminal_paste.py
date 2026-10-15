"""Dictations + "submit" into Windows Terminal arrive whole, however long.

Windows Terminal stops a multi-line paste with a "you are about to paste
multiple lines" warning. A long dictation (the cleanup splits it into
paragraphs) sat behind it, and the "submit" Enter only clicked "Paste
anyway". Over 5 KiB it asks again ("paste more than 5 KiB?"), and LocalFlow
now presses its "Paste anyway" itself. This opens a new Windows Terminal window
running a tiny program
that records each line it receives, pastes three paragraphs into it the way
always-on does (paste where I started, then submit), and checks one
submitted line came through. Briefly shows a terminal window.

    uv run python tests/terminal_paste.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow import desktop, window  # noqa: E402
from localflow.commands import BY_NAME, Ctx  # noqa: E402
from localflow.config import Config  # noqa: E402
from localflow.engine import Engine, Result  # noqa: E402

TEXT = ("First paragraph about the plan for today.\n\n"
        "Second paragraph with more detail.\n\n"
        "Third and last paragraph.")


def main() -> int:
    if not shutil.which("wt"):
        print("Windows Terminal (wt.exe) not found: nothing to test")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="localflow-term-"))
    out = tmp / "lines.txt"
    reader = tmp / "reader.py"
    reader.write_text(
        "import sys\n"
        f"out = open(r'{out}', 'a', encoding='utf-8')\n"
        "print('LocalFlow terminal test - waiting for a line')\n"
        "for line in sys.stdin:\n"
        "    out.write(line.rstrip('\\n') + '\\n'); out.flush()\n",
        encoding="utf-8")
    before = {w.hwnd for w in desktop.windows() if w.exe == "windowsterminal"}
    subprocess.Popen(["wt", "-w", "new", "--title", "LocalFlow terminal test",
                      sys.executable, str(reader)])
    hwnd = 0
    for _ in range(100):
        new = [w for w in desktop.windows() if w.exe == "windowsterminal" and w.hwnd not in before]
        if new:
            hwnd = new[0].hwnd
            break
        time.sleep(0.1)
    failures = 0

    def expect(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += not ok
        print(f"  {'pass' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")

    try:
        expect("terminal window opened", bool(hwnd))
        if not hwnd:
            return 1
        time.sleep(2.0)  # Python starting inside it
        cfg = Config()
        cfg.history.enabled = False
        cfg.inject.paste_to = "start"
        engine = Engine(cfg)
        engine.process = lambda audio, **kw: Result(  # noqa: E731
            text=TEXT, raw=TEXT, audio_seconds=1, transcribe_seconds=0, cleanup_seconds=0)
        target = window.Target(hwnd, None, "")
        window.activate(target)
        time.sleep(0.3)
        engine._process_and_inject(
            np.zeros(16000, np.float32), True, True, "test", None,
            lambda t: BY_NAME["submit"].action("", Ctx(last_any=t, last_at=time.time())),
            target=target)
        time.sleep(1.5)
        lines = out.read_text(encoding="utf-8").splitlines() if out.exists() else []
        expect("one line arrived, submitted", len(lines) == 1, repr(lines))
        expect("all three paragraphs in it",
               bool(lines) and "First" in lines[0] and "Third and last" in lines[0], repr(lines[:1]))

        # Over 5 KiB: Windows Terminal's "are you sure?" is answered for you.
        big = " ".join(f"word{i}" for i in range(1100))  # ~7.4 KB
        engine.process = lambda audio, **kw: Result(  # noqa: E731
            text=big, raw=big, audio_seconds=1, transcribe_seconds=0, cleanup_seconds=0)
        # in_target brings the test window forward and checks it really is in
        # front before pasting (it never pastes into a different window).
        if True:
            engine._process_and_inject(
                np.zeros(16000, np.float32), True, True, "test", None,
                lambda t: BY_NAME["submit"].action("", Ctx(last_any=t, last_at=time.time())),
                target=target)
            time.sleep(2.0)
            lines = out.read_text(encoding="utf-8").splitlines() if out.exists() else []
            expect("7 KB dictation: warning answered, arrived whole, submitted",
                   len(lines) == 2 and lines[1] == big, f"{len(lines)} lines, last {len(lines[-1]) if lines else 0} chars")

        # "paste" in command mode, with a big clipboard.
        from localflow.inject import set_clipboard_text

        set_clipboard_text("x" * 6000)
        window.activate(target)
        time.sleep(0.3)
        if desktop.foreground() != hwnd:
            print("  SKIP  'paste' command: the test window isn't in front (PC in use?)")
        else:
            msg = BY_NAME["paste"].action("", Ctx())
            time.sleep(0.3)
            desktop.press("enter")
            time.sleep(1.5)
            lines = out.read_text(encoding="utf-8").splitlines() if out.exists() else []
            expect("'paste' command: warning answered", msg.startswith("Pasted (said yes")
                   and lines[-1:] == ["x" * 6000], f"{msg!r}, last line {len(lines[-1]) if lines else 0} chars")
    finally:
        if hwnd:
            desktop.close(hwnd)
    print("\nall passed" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
