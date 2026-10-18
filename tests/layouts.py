"""Arranging windows and saved layouts.

The first part needs no windows: the tiling sums, what the phrases parse to,
and saving / finding layouts in a scratch file, never yours. With --live it
also opens three windows of its own and arranges, saves and restores those
(only those: your windows are never moved).

    uv run python tests/layouts.py [--live]
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow import commands, desktop  # noqa: E402
from localflow.commands import BY_NAME, Ctx, Grammar, Parsed  # noqa: E402

failures = 0


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


print("tiling")
area = (0, 0, 1920, 1040)
for n, shape in ((1, [1]), (2, [2]), (3, [3]), (4, [2, 2]), (5, [3, 2]), (6, [3, 3])):
    tiles = desktop.grid(n, area)
    rows = sorted({t[1] for t in tiles})
    got = [sum(1 for t in tiles if t[1] == r) for r in rows]
    covered = sum((r - l) * (b - t) for l, t, r, b in tiles)
    expect(f"{n} windows: rows of {shape}, the whole screen used", got == shape
           and covered == 1920 * 1040, f"{got} {covered}")
tall = desktop.grid(2, (0, 0, 1080, 1920))
expect("a portrait screen stacks them", tall[0][1] != tall[1][1], str(tall))

print("window chips (show windows)")
from ctypes import wintypes  # noqa: E402

real = {k: getattr(desktop, k) for k in ("window_rect", "monitors", "monitor_of")}
rects = {1: (100, 100, 900, 700), 2: (100, 100, 900, 700),  # stacked at one spot
         3: (-8, -8, 1928, 1048),  # maximised: its corner is just off screen
         4: (1000, 200, 1600, 800)}  # behind window 3: chipped all the same
desktop.window_rect = lambda h: rects[h]
desktop.monitors = lambda: [(1, wintypes.RECT(0, 0, 1920, 1080), wintypes.RECT(0, 0, 1920, 1040))]
desktop.monitor_of = lambda h, mons=None: 1
real_iconic = desktop.user32.IsIconic
desktop.user32.IsIconic = lambda h: False
chips = dict(desktop.window_chips([desktop.Win(h, "t", "x", "") for h in rects]))
desktop.user32.IsIconic = real_iconic
for k, v in real.items():
    setattr(desktop, k, v)
expect("every window gets a chip, covered or not", sorted(chips) == [1, 2, 3, 4], str(chips))
boxes = list(chips.values())
expect("...no two chips on top of each other",
       not any(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
               for i, a in enumerate(boxes) for b in boxes[i + 1:]), str(chips))
expect("...all on screen", all(b[0] >= 0 and b[1] >= 0 and b[2] <= 1920 and b[3] <= 1040
                               for b in boxes), str(chips))

print("phrases")
g = Grammar()  # no learned phrases
cases = {
    "arrange windows": ("arrange", ""), "tile all my windows": ("arrange", ""),
    "organize the windows": ("arrange", ""), "put my windows side by side": ("arrange", ""),
    "save layout as work": ("save_layout", "work"), "save the window layout": ("save_layout", ""),
    "restore layout work": ("restore_layout", "work"), "layout gaming": ("restore_layout", "gaming"),
    "load my layout work": ("restore_layout", "work"), "delete layout work": ("delete_layout", "work"),
    "list layouts": ("list_layouts", ""),
    "show windows": ("show_windows", ""),  # still the window list
}
for said, (name, arg) in cases.items():
    p = g.parse(said.split())
    expect(f"'{said}'", p is not None and p.command.name == name and p.arg == arg, str(p))

print("saving and finding layouts (scratch file)")
commands.LAYOUTS = Path(tempfile.mkdtemp()) / "layouts.json"
fake = [{"hwnd": 1, "exe": "notepad", "path": "", "title": "a", "show": 1, "flags": 0,
         "min": [0, 0], "max": [0, 0], "normal": [0, 0, 10, 10], "rect": [0, 0, 10, 10]}]
desktop.snapshot = lambda: list(fake)
restored = []
desktop.restore = lambda entries, launch_missing=True, wait=10.0: (
    restored.append((entries, launch_missing)) or (len(entries), 0, []))
try:
    commands._restore_layout("work", Ctx())
    expect("no layouts: an error", False)
except commands.CommandError as exc:
    expect("no layouts: says how to save one", "save layout as" in str(exc), str(exc))
print("   ", commands._save_layout("work", Ctx()))
print("   ", commands._save_layout("", Ctx()))
expect("saved under its name, and 'default' without one",
       set(commands.load_layouts()) == {"work", "default"}, str(commands.load_layouts()))
expect("a near miss finds it ('works')", commands.find_layout("works") == "work")
expect("...but not something else", commands.find_layout("gaming") is None)
expect("restore_layout is only a command when the layout exists",
       commands.resolvable(Parsed(BY_NAME["restore_layout"], "work", 1.0))
       and not commands.resolvable(Parsed(BY_NAME["restore_layout"], "the house", 1.0)))
desktop.windows = lambda: []
print("   ", commands._restore_layout("work", Ctx()))
expect("restore puts back what was saved", restored and restored[-1][0] == fake, str(restored))
print("   ", commands._delete_layout("work", Ctx()))
expect("delete removes it", set(commands.load_layouts()) == {"default"})

print("undo puts every window back")
restored.clear()
step = commands.undo_step(Parsed(BY_NAME["arrange"], "", 1.0), Ctx(), 0, 0, 0, fake, "ok")
step.run()
expect("...without opening anything", restored == [(fake, False)], str(restored))
expect("plain 'undo' takes it back", "arrange" in commands.SMART_UNDO
       and "restore_layout" in commands.SMART_UNDO)

if "--live" in sys.argv:
    import importlib
    import tkinter as tk

    importlib.reload(desktop)
    desktop.per_monitor_dpi()
    print("live: three windows of our own")
    root = tk.Tk()
    root.withdraw()
    tops = []
    for i in range(3):
        t = tk.Toplevel(root)
        t.title(f"layout test {i}")
        t.geometry(f"300x200+{100 + 40 * i}+{100 + 40 * i}")
        tops.append(t)
    root.update()
    time.sleep(0.5)
    root.update()
    ours = [w for w in desktop.windows() if w.title.startswith("layout test")]
    expect("found them", len(ours) == 3, str(len(ours)))
    before = {w.hwnd: desktop.window_rect(w.hwnd) for w in ours}
    snap = [e for e in desktop.snapshot() if e["title"].startswith("layout test")]
    n = desktop.arrange(ours)
    root.update()
    time.sleep(0.4)
    after = {w.hwnd: desktop.window_rect(w.hwnd) for w in ours}
    mon = desktop.monitor_of(ours[0].hwnd)
    work = desktop.monitors()[mon - 1][2]
    widths = [r - l for l, t, r, b in after.values()]
    expect("arranged: three side by side, each about a third of the screen",
           n == 3 and all(abs(w - (work.right - work.left) / 3) < 40 for w in widths),
           f"{n} {after}")

    def overlap(a, b):
        return max(0, min(a[2], b[2]) - max(a[0], b[0]) - 20) * max(0, min(a[3], b[3]) - max(a[1], b[1]) - 20)

    rects = list(after.values())
    expect("...none covering another", all(overlap(rects[i], rects[j]) == 0
                                           for i in range(3) for j in range(i + 1, 3)))
    placed, opened, missing = desktop.restore(snap, launch_missing=False)
    root.update()
    time.sleep(0.3)
    back = {w.hwnd: desktop.window_rect(w.hwnd) for w in ours}
    expect("restore puts each exactly back", placed == 3 and back == before, f"{back} {before}")
    root.destroy()

print("\nall passed" if not failures else f"\n{failures} FAILED")
