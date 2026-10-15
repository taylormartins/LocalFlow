"""Voice commands for real: switch, click by name or number, center, minimize, close.

Opens a small Windows Forms window ("LocalFlow test Fennel", with an
"Approve" button) in a separate process, then runs the parsed commands
against it exactly as command mode would, and checks what happened. With
Ollama running, also checks the LLM layer picks known commands only.
Takes a few seconds and briefly shows the window. Run it while not using
the PC: it acts on the window in front, and stops rather than click in
anything but its own.

    uv run python tests/desktop_commands.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
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


def written(path: Path, timeout: float = 5.0) -> bool:
    """The test window's button handler runs in PowerShell, which can take a
    couple of seconds to write its file the first time."""
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    return path.exists()


def monitors() -> list[tuple[int, int, int, int, int]]:
    """(left, top, right, bottom, dpi) of each monitor, real pixels."""
    from ctypes import wintypes

    desktop.per_monitor_dpi()
    out = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def each(h, _hdc, r, _l):
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(h, 0, ctypes.byref(dx), ctypes.byref(dy))
        out.append((r.contents.left, r.contents.top, r.contents.right, r.contents.bottom, dx.value))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(0, 0, proc(each), 0)
    return out


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
        if desktop.foreground() != hwnd:
            print("  SKIP  the rest: the test window lost focus (PC in use?) - rerun when idle")
            return 1
        r = say("click approve", wait=1.0)
        expect("'click approve' presses the button", written(out), str(r))
        r = say("click the purple elephant")
        expect("a button that isn't there is refused", r is not None and not r[2], str(r))

        print("center / numbered labels")
        from localflow import commands as cmds
        from localflow.engine import Engine
        from localflow.overlay import Overlay

        cfg0 = Config()
        cfg0.history.enabled = False
        overlay = Overlay(Engine(cfg0), cfg0.overlay)
        overlay.start()
        time.sleep(1.5)
        r = say("center fennel")
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = desktop.work_area(hwnd)
        mid_x = (rect.left + rect.right) // 2 - (area.left + area.right) // 2
        mid_y = (rect.top + rect.bottom) // 2 - (area.top + area.bottom) // 2
        expect("'center fennel' centres it", abs(mid_x) <= 2 and abs(mid_y) <= 2,
               f"{r} off by {mid_x},{mid_y}")
        print("place / undo")
        desktop.per_monitor_dpi()
        cmds._undo.clear()
        before = desktop.window_rect(hwnd)
        r = say("move fennel to the top right")
        a = desktop.work_area(hwnd)
        got = desktop.window_rect(hwnd)
        want = ((a.left + a.right) // 2, a.top, a.right, (a.top + a.bottom) // 2)
        expect("'move fennel to the top right' fills that quarter of its own monitor",
               all(abs(x - y) <= 12 for x, y in zip(got, want)), f"{r} {got} vs {want}")
        r = say("undo last command")
        expect("'undo last command' puts it back", desktop.window_rect(hwnd) == before,
               f"{r} {desktop.window_rect(hwnd)} vs {before}")
        r = say("undo last command")
        expect("nothing left to undo is said, not guessed", r is not None and not r[2], str(r))
        before = desktop.window_rect(hwnd)
        say("maximize fennel")
        r = say("undo")
        expect("plain 'undo' right after a window command undoes it",
               not user32.IsZoomed(hwnd) and desktop.window_rect(hwnd) == before, str(r))

        print("labels on another monitor's scaling")
        mons = monitors()
        other = next((m for m in mons if m[4] != mons[0][4]), None)
        if other:
            user32.SetWindowPos(hwnd, 0, other[0] + 100, other[1] + 200, 0, 0, 0x0001 | 0x0004)
            time.sleep(0.4)
            say("switch to fennel")
        out.unlink(missing_ok=True)
        r = say("show me numbered click labels on screen in fennel", wait=0.6)
        names = [c.name for c in cmds._labelled]
        if desktop.foreground() != hwnd:
            # Someone is using the PC and took focus: never click in their window.
            cmds.hide_labels()
            print("  SKIP  label clicks: the test window lost focus (PC in use?)")
            names = []
        expect("labels shown", r is not None and r[2] and "Approve" in names, f"{r} {names}")
        expect("label layer visible", bool(user32.IsWindowVisible(overlay.label_hwnd)))
        if "Approve" in names:
            # The drawn outline must sit on the control's real position.
            from localflow import commands as c2

            ctl = c2._labelled[names.index("Approve")]
            uia, _ = desktop._automation()
            real = ctl.element.CurrentBoundingRectangle
            expect("label position = the button's real position (mixed DPI)",
                   (real.left, real.top, real.right, real.bottom) == ctl.rect,
                   f"{ctl.rect} vs {(real.left, real.top, real.right, real.bottom)}")
            shot = tmp / "labels.png"
            try:
                from PIL import ImageGrab

                l, t, rr, b = desktop.window_rect(hwnd)
                ImageGrab.grab((l - 10, t - 10, rr + 10, b + 10), all_screens=True).save(shot)
                print(f"    (screenshot: {shot})")
            except Exception as exc:
                print(f"    (no screenshot: {exc})")
        if "Approve" in names and desktop.foreground() == hwnd:
            n = names.index("Approve") + 1
            r = say(f"click {n}", wait=1.0)
            expect(f"'click {n}' presses Approve", written(out), str(r))
            expect("labels gone after the click",
                   not cmds._labelled and not user32.IsWindowVisible(overlay.label_hwnd))
        r = say("click 5")
        expect("a number with no labels up is refused", r is not None and not r[2], str(r))
        p = grammar.parse(normalise("open a new tab in fennel"))
        expect("'open a new tab in fennel' -> new tab, aimed at fennel",
               p is not None and p.command.name == "new_tab" and p.where == "fennel", str(p))

        print("minimize / restore / close")
        r = say("minimize fennel")
        expect("'minimize fennel'", bool(user32.IsIconic(hwnd)), str(r))
        r = say("restore fennel")
        expect("'restore fennel'", not user32.IsIconic(hwnd), str(r))
        r = say("switch to the purple elephant")
        expect("a window that isn't there is refused", r is not None and not r[2], str(r))
        r = say("close fennel", wait=1.5)
        expect("'close fennel'", not user32.IsWindow(hwnd), str(r))

        print("history")
        from localflow import history

        store = tmp / "history.jsonl"
        for i, text in enumerate(["Book the meeting room for Thursday.", "Call the dentist.",
                                  "Thursday's budget meeting moved to Friday."]):
            history.add(history.Entry(1000.0 + i, text), path=store)
        found = [e.text for e in history.search(history.load(store), "thursday meeting")]
        expect("search: every word, newest first",
               found == ["Thursday's budget meeting moved to Friday.",
                         "Book the meeting room for Thursday."], str(found))
        history.remove(1001.0, path=store)
        expect("delete one", len(history.load(store)) == 2)

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
