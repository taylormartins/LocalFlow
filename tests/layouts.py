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
three = desktop.grid(3, (0, 0, 1440, 2500))


def same_size(tiles):  # to the pixel the screen divides into
    ws, hs = [r - l for l, t, r, b in tiles], [b - t for l, t, r, b in tiles]
    return max(ws) - min(ws) <= 2 and max(hs) - min(hs) <= 2


expect("three on a portrait screen: stacked, all the same size (not two and one wide)",
       len({t[1] for t in three}) == 3 and same_size(three), str(three))
three = desktop.grid(3, (0, 0, 3840, 2088))
expect("three on a 4K screen: side by side, all the same size",
       len({t[1] for t in three}) == 1 and same_size(three), str(three))

print("arrangement styles")
wide = (0, 0, 2560, 1040)
expect("columns: one row", len({t[1] for t in desktop.grid(4, wide, "columns")}) == 1)
expect("rows: one column", len({t[0] for t in desktop.grid(4, wide, "rows")}) == 1)
main = desktop.grid(4, wide, "main")
expect("main: the first window gets 60 %, the rest stack beside it",
       main[0] == (0, 0, 1536, 1040) and all(t[0] == 1536 for t in main[1:]), str(main))
casc = desktop.grid(3, wide, "cascade")
expect("cascade: overlapping, each a step down and right, all on screen",
       casc[1][0] > casc[0][0] and casc[1][1] > casc[0][1]
       and all(r <= 2560 and b <= 1040 for _, _, r, b in casc), str(casc))
for style in desktop.STYLES:
    for n in (1, 2, 5, 9):
        tiles = desktop.grid(n, wide, style)
        if len(tiles) != n or not all(0 <= l < r <= 2560 and 0 <= t < b <= 1040
                                      for l, t, r, b in tiles):
            expect(f"{style} with {n} windows: {n} tiles on screen", False, str(tiles))

if sys.platform != "win32":
    print("window chips, moving and swapping: Windows window control (skipped here)")
else:
    print("window chips (show windows)")
    from localflow.platform import wintypes  # noqa: E402

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

    print("windows that fight back (move_windows, modelled)")
    real = {k: getattr(desktop, k) for k in ("window_rect", "_borders", "_exe_name")}
    real_swp = desktop.user32.SetWindowPos
    rects = {}
    models = {
        "obeys": lambda x, y, w, h: (x, y, x + w, y + h),
        "shifts itself 300 px": lambda x, y, w, h: (x + 300, y, x + 300 + w, y + h),
        "scales positions 1.5x (the WinForms window)": lambda x, y, w, h: (
            round(x * 1.5), round(y * 1.5), round(x * 1.5) + w, round(y * 1.5) + h),
        "has a minimum size": lambda x, y, w, h: (x, y, x + max(w, 980), y + max(h, 820)),
    }
    names = list(models)
    desktop.window_rect = lambda h: rects[h]
    desktop._borders = lambda h: (0, 0, 0, 0)
    desktop._exe_name = lambda h: names[h]
    desktop.user32.SetWindowPos = lambda h, after, x, y, w, hh, flags: rects.__setitem__(
        h, models[names[h]](x, y, w, hh))
    target = (1200, 300, 600, 400)
    placed = desktop.move_windows([(i, target) for i in range(len(names))], settle=0.0)
    for i, name in enumerate(names):
        l, t = rects[i][:2]
        expect(f"{name}: ends where it was put", abs(l - 1200) <= 6 and abs(t - 300) <= 6,
               str(rects[i]))
    expect("...all counted as placed", placed == len(names), str(placed))
    for k, v in real.items():
        setattr(desktop, k, v)
    desktop.user32.SetWindowPos = real_swp

    print("swapping two windows (modelled)")
    real = {k: getattr(desktop, k) for k in ("window_rect", "_borders", "monitors", "monitor_of")}
    real_u = {k: getattr(desktop.user32, k) for k in ("SetWindowPos", "IsZoomed", "IsIconic",
                                                      "ShowWindow")}
    rects = {1: (0, 0, 800, 600), 2: (1000, 100, 1600, 500), 3: (2000, 0, 2800, 600)}
    zoomed = {1: False, 2: False, 3: True}
    shown = []
    desktop.window_rect = lambda h: rects[h]
    desktop._borders = lambda h: (0, 0, 0, 0)
    desktop.monitors = lambda: [(1, wintypes.RECT(0, 0, 1920, 1080), wintypes.RECT(0, 0, 1920, 1040)),
                                (2, wintypes.RECT(1920, 0, 3840, 1080),
                                 wintypes.RECT(1920, 0, 3840, 1040))]
    desktop.monitor_of = lambda h, mons=None: 1 if rects[h][0] < 1920 else 2
    desktop.user32.SetWindowPos = lambda h, after, x, y, w, hh, f: rects.__setitem__(h, (x, y, x + w, y + hh))
    desktop.user32.IsZoomed = lambda h: zoomed.get(h, False)
    desktop.user32.IsIconic = lambda h: False
    desktop.user32.ShowWindow = lambda h, how: shown.append((h, how))
    desktop.swap_windows(1, 2)
    expect("two windows trade places and sizes",
           rects[1] == (1000, 100, 1600, 500) and rects[2] == (0, 0, 800, 600), str(rects))
    shown.clear()
    desktop.swap_windows(2, 3)  # 3 is maximised on screen 2
    expect("a maximised one: the other takes its screen, maximised; it takes the other's spot",
           rects[3] == (0, 0, 800, 600) and (2, desktop.SW_MAXIMIZE) in shown
           and rects[2][0] >= 1920, f"{rects} {shown}")
    for k, v in real.items():
        setattr(desktop, k, v)
    for k, v in real_u.items():
        setattr(desktop.user32, k, v)
    expect("swapping can be undone", "swap" in commands.REARRANGES)

print("phrases")
g = Grammar()  # no learned phrases
cases = {
    "arrange windows": ("arrange", ""), "tile all my windows": ("arrange", ""),
    "organize the windows": ("arrange", ""), "put my windows side by side": ("arrange_columns", ""),
    "save layout as work": ("save_layout", "work"), "save the window layout": ("save_layout", ""),
    "restore layout work": ("restore_layout", "work"), "layout gaming": ("restore_layout", "gaming"),
    "load my layout work": ("restore_layout", "work"), "delete layout work": ("delete_layout", "work"),
    "list layouts": ("list_layouts", ""),
    "show windows": ("show_windows", ""),  # still the window list
    "arrange windows on monitor 3": ("arrange", "3"),
    "arrange windows in columns": ("arrange_columns", ""),
    "stack my windows": ("arrange_rows", ""),
    "arrange the windows in rows on screen 2": ("arrange_rows", "2"),
    "arrange windows in a grid": ("arrange_grid", ""),
    "main window": ("arrange_main", ""), "cascade windows": ("arrange_cascade", ""),
    "next arrangement": ("arrange_next", ""), "shuffle my windows": ("arrange_next", ""),
    "swap 2 and 4": ("swap", "2 and 4"), "switch window 4 and 2": ("swap", "4 and 2"),
    "swap chrome with spotify": ("swap", "chrome with spotify"),
    "switch windows": ("switch_window", ""),
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
expect("every arrangement style can be undone", all(
    n in commands.REARRANGES for n in commands.BY_NAME if n.startswith("arrange")))
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
