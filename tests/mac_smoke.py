"""The Mac-only pieces, on a real Mac (CI's macos-latest has a screen):
overlay windows, screens, apps, the window list, the clipboard. Skips
elsewhere. Needs no permissions: nothing is typed or clicked.

    uv run python tests/mac_smoke.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from localflow.platform import IS_MAC, tk_first  # noqa: E402

failures = 0


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


if not IS_MAC:
    print("  skip  not a Mac")
    print("\nall passed")
    sys.exit(0)

root = tk_first()  # before anything touches AppKit
import tkinter as tk  # noqa: E402

from localflow import desktop, inject, layers  # noqa: E402

print("overlay windows")
win = tk.Toplevel(root)
win.overrideredirect(True)
layers.transparent(win)
c = tk.Canvas(win, width=200, height=50, bg=layers.KEY, highlightthickness=0)
c.pack()
c.create_oval(10, 10, 40, 40, fill="#d4af37")
win.update()
h = layers.prepare(win)
ns = win._lf_ns
expect("its NSWindow is found", ns is not None)
expect("click-through", bool(ns.ignoresMouseEvents()))
expect("floats above other windows", ns.level() >= 25, str(ns.level()))
expect("hidden after prepare", not layers.visible(h))
layers.show(h, 120, 140, 200, 50)
root.update()
time.sleep(0.2)
expect("shown", layers.visible(h))
expect("where it was put", (win.winfo_x(), win.winfo_y()) == (120, 140),
       str((win.winfo_x(), win.winfo_y())))
layers.raise_(h)
layers.hide(h)
root.update()
expect("hidden again", not layers.visible(h))
area = layers.work_area()
expect("a work area", area.right > area.left and area.bottom > area.top, str(
    (area.left, area.top, area.right, area.bottom)))

print("screens, apps, windows")
mons = desktop.monitors()
expect("at least one screen", len(mons) >= 1, str(mons))
apps = desktop.apps()
expect("installed apps are listed (Safari among them)", any(a.name == "Safari" for a in apps),
       str([a.name for a in apps][:8]))
app, score = desktop.find_app("safari")
expect("'open safari' finds it", app is not None and app.name == "Safari", str(app))
wins = desktop.windows()
expect("the window list works (may be empty on CI)", isinstance(wins, list))
expect("the front app's window number decodes to its process",
       desktop.foreground() == 0 or desktop.pid_of(desktop.foreground()) > 0)

print("the clipboard")
before = inject.get_clipboard_text()
inject.set_clipboard_text("Golden Flow ✓ clipboard")
expect("round trip (UTF-8)", inject.get_clipboard_text() == "Golden Flow ✓ clipboard",
       repr(inject.get_clipboard_text()))
if before is not None:
    inject.set_clipboard_text(before)

print("Windows shortcuts become Mac ones")
from localflow.mac.desktop import MAC_CHORDS  # noqa: E402

expect("copy, new tab, back", MAC_CHORDS["ctrl+c"] == "cmd+c" and MAC_CHORDS["ctrl+t"] == "cmd+t"
       and MAC_CHORDS["alt+left"] == "cmd+[")
root.destroy()
print("\nall passed" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
