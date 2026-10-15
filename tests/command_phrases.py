"""How spoken commands are understood, using phrases from real logs.

Every line here is something someone actually said (or how Whisper heard
it) that once failed. Each must parse to the command (and name) shown.
Pure parsing: no windows are touched, nothing is pressed. Runs in a second.

    uv run python tests/command_phrases.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow.commands import Grammar  # noqa: E402
from localflow.wake import normalise  # noqa: E402

#: (heard, command, arg, where); None = must NOT be a command.
CASES = [
    # Enter after a dictation, heard every which way.
    ("press enter", "submit", "", ""),
    ("press into", "submit", "", ""),
    ("pressing", "submit", "", ""),
    ("presenter", "submit", "", ""),
    ("summit", "submit", "", ""),
    # Bringing a window forward.
    ("switch to chrome", "switch", "chrome", ""),
    ("select filezilla", "switch", "filezilla", ""),
    ("pull up filezilla window", "switch", "filezilla", ""),
    ("pull up the filezilla window", "switch", "filezilla", ""),
    ("show me spotify", "switch", "spotify", ""),
    ("take me to outlook", "switch", "outlook", ""),
    # A name spelled out because the word kept being misheard.
    ("select n o t i o n", "switch", "notion", ""),
    ("select no shun n o t i o n", "switch", "notion", ""),
    # Aimed at a window.
    ("new chrome tab", "new_tab", "", "chrome"),
    ("open a new chrome tab", "new_tab", "", "chrome"),
    ("new tab", "new_tab", "", ""),
    # Windows and placement.
    ("move it to the top right", "place_top_right", "", ""),
    ("move chrome to the bottom left corner", "place_bottom_left", "chrome", ""),
    ("bring my chrome twitch window to the center of my monitor", "center", "chrome twitch", ""),
    ("go to the top", "top", "", ""),
    # Telling look-alike windows apart: the numbered window list.
    ("show windows", "show_windows", "", ""),
    ("show me all the open windows", "show_windows", "", ""),
    ("switch to 3", "switch", "3", ""),
    ("close 2", "close_window", "2", ""),
    ("move 2 to the top right", "place_top_right", "2", ""),
    # Monitors by number.
    ("move it to monitor 2", "to_monitor", "2", ""),
    ("move it to the top right of monitor 3", "place_top_right_of", "3", ""),
    ("show monitors", "show_monitors", "", ""),
    # Labels, undo, sleep.
    ("show me numbered click labels on screen", "show_labels", "", ""),
    ("click 17", "click", "17", ""),
    ("undo last command", "undo_command", "", ""),
    ("put the computer to sleep", "sleep", "", ""),
    # Talk, not commands.
    ("i will switch to chrome later", None, "", ""),
    ("submit the form tomorrow after lunch", None, "", ""),
]


def main() -> int:
    g = Grammar()
    failures = 0
    for heard, command, arg, where in CASES:
        p = g._parse(normalise(heard)) if " in " not in f" {heard} " else g.parse(normalise(heard))
        got = (p.command.name, p.arg, p.where) if p else None
        want = (command, arg, where) if command else None
        ok = got == want
        failures += not ok
        print(f"  {'pass' if ok else 'FAIL'}  {heard!r}: {got}" + ("" if ok else f"  (want {want})"))
    print("\nall passed" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
