"""Voice commands for real: switch, click by name, minimize, restore, close.

Opens a small Windows Forms window ("LocalFlow test Fennel", with an
"Approve" button) in a separate process, then runs the parsed commands
against it exactly as command mode would, and checks what happened. With
Ollama running, also checks the LLM layer picks known commands only.
Takes a few seconds and briefly shows the window.

    uv run python tests/desktop_commands.py
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow import desktop  # noqa: E402
from localflow.commands import Ctx, Grammar, Runner, plan_with_llm  # noqa: E402
from localflow.config import Config  # noqa: E402
from localflow.wake import normalise  # noqa: E402

FORM = r'''
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object Windows.Forms.Form
$f.Text = "LocalFlow test Fennel"; $f.Width = 360; $f.Height = 140
$f.StartPosition = "Manual"; $f.Left = 200; $f.Top = 300
$b = New-Object Windows.Forms.Button
$b.Text = "Approve"; $b.Left = 20; $b.Top = 20; $b.Width = 120
$b.Add_Click({ Set-Content -Path "%OUT%" -Value "approved" })
$f.Controls.Add($b)
[Windows.Forms.Application]::Run($f)
'''


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="localflow-desktop-"))
    out = tmp / "clicked.txt"
    script = tmp / "form.ps1"
    script.write_text(FORM.replace("%OUT%", str(out)), encoding="ascii")
    proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                             "-WindowStyle", "Hidden", "-File", str(script)])
    user32 = ctypes.windll.user32
    grammar = Grammar()
    done: list[tuple[str, str, bool]] = []
    runner = Runner(lambda p, msg, ok: done.append((p.command.name, msg, ok)))
    failures = 0

    def expect(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += not ok
        print(f"  {'pass' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")

    def say(text: str, wait: float = 0.8) -> tuple[str, str, bool] | None:
        p = grammar.parse(normalise(text))
        if p is None:
            return None
        n = len(done)
        runner.submit(p, Ctx())
        deadline = time.monotonic() + 8
        while len(done) == n and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(wait)
        return done[-1] if len(done) > n else None

    try:
        hwnd = 0
        for _ in range(100):
            hwnd = user32.FindWindowW(None, "LocalFlow test Fennel")
            if hwnd:
                break
            time.sleep(0.1)
        expect("test window up", bool(hwnd))
        time.sleep(0.5)

        print("switch / click")
        r = say("switch to fennel")
        expect("'switch to fennel' brings it forward", desktop.foreground() == hwnd, str(r))
        r = say("click approve", wait=1.0)
        expect("'click approve' presses the button", out.exists(), str(r))
        r = say("click the purple elephant")
        expect("a button that isn't there is refused", r is not None and not r[2], str(r))

        print("minimize / restore / close")
        r = say("minimize fennel")
        expect("'minimize fennel'", bool(user32.IsIconic(hwnd)), str(r))
        r = say("restore fennel")
        expect("'restore fennel'", not user32.IsIconic(hwnd), str(r))
        r = say("switch to the purple elephant")
        expect("a window that isn't there is refused", r is not None and not r[2], str(r))
        r = say("close fennel", wait=1.5)
        expect("'close fennel'", not user32.IsWindow(hwnd), str(r))

        print("app list")
        app, score = desktop.find_app("calculator")
        expect("'calculator' is in the app list", app is not None and score >= 0.9,
               f"{app.name if app else None} {score:.2f}")

        cfg = Config()
        import httpx

        client = httpx.Client(base_url=cfg.cleanup.host, timeout=30)
        try:
            client.get("/api/tags", timeout=2).raise_for_status()
            llm = True
        except Exception:
            llm = False
        if llm:
            print(f"LLM layer ({cfg.cleanup.model})")
            plan = plan_with_llm(client, cfg.cleanup.model, "could you get rid of this tab for me")
            expect("'get rid of this tab' -> close_tab", [p.command.name for p in plan] == ["close_tab"],
                   ", ".join(map(str, plan)))
            plan = plan_with_llm(client, cfg.cleanup.model, "what's the weather like in Paris")
            expect("a request no command covers -> nothing", plan == [], ", ".join(map(str, plan)))
        else:
            print("(Ollama not running: LLM layer not checked)")
    finally:
        proc.kill()

    print("\nall passed" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
