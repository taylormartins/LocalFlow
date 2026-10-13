"""Exercise the hotkey state machine in both modes.

    uv run python tests/hotkeys.py

WARNING: this synthesises real F9/F10/Escape key presses. They are suppressed by
the listener under test, but run it on a desktop you are not actively using and
close the LocalFlow tray app first, or two listeners will fight over the keys.

Covers the three bugs found the hard way:
  1. pynput's suppress_event() raises, so on_press/on_release never fire for a
     suppressed key - all state tracking has to live in the win32 event filter.
  2. In toggle mode, tapping F9 then F10 used to leave a recording that neither
     key could stop.
  3. Escape must cancel an armed toggle without being suppressed globally.
"""

import sys
import time

from pynput.keyboard import Controller, Key

from localflow.hotkey import Binding, HotkeyManager

kb = Controller()


def tap(key, pause: float = 0.35) -> None:
    kb.press(key)
    time.sleep(0.05)
    kb.release(key)
    time.sleep(pause)


def check(label: str, got: list[str], want: list[str]) -> bool:
    ok = got == want
    print(f"  {'pass' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got}\n        want {want}")
    return ok


def main() -> int:
    events: list[str] = []
    failures = 0

    # --- hold mode -------------------------------------------------------
    m = HotkeyManager()
    m.bind(Binding("<f9>", lambda: events.append("start"), lambda: events.append("stop")))
    m.start()
    time.sleep(0.4)

    print("hold mode:")
    kb.press(Key.f9)
    time.sleep(0.6)
    kb.release(Key.f9)
    time.sleep(0.4)
    failures += not check("press and release", events, ["start", "stop"])
    m.stop()

    # --- toggle mode -----------------------------------------------------
    events.clear()
    m = HotkeyManager()
    m.bind(Binding("<f9>", lambda: events.append("F9-start"),
                   lambda: events.append("F9-stop"), mode="toggle"))
    m.bind(Binding("<f10>", lambda: events.append("F10-start"),
                   lambda: events.append("F10-stop"), mode="toggle"))
    m.bind(Binding("<esc>", lambda: events.append("CANCEL"), mode="action", suppress=False))
    m.start()
    time.sleep(0.4)

    print("toggle mode:")
    tap(Key.f9)
    tap(Key.f9)
    failures += not check("tap twice starts then stops", events, ["F9-start", "F9-stop"])

    events.clear()
    kb.press(Key.f9)
    time.sleep(0.8)
    kb.release(Key.f9)
    time.sleep(0.3)
    failures += not check("holding the key does not stop it", events, ["F9-start"])
    tap(Key.f9)  # close it

    events.clear()
    tap(Key.f9)
    tap(Key.f10)
    failures += not check(
        "F9 then F10 closes the first", events, ["F9-start", "F9-stop", "F10-start"]
    )
    tap(Key.f10)

    events.clear()
    tap(Key.f9)
    tap(Key.esc)
    failures += not check("escape cancels", events, ["F9-start", "CANCEL"])

    events.clear()
    tap(Key.f9)
    failures += not check("F9 restarts cleanly after cancel", events, ["F9-start"])
    tap(Key.f9)

    m.stop()
    print("\nall passed" if not failures else f"\n{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
