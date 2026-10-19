"""Start at login on a Mac: a launchd agent (Windows uses scheduled tasks).

~/Library/LaunchAgents/com.goldenflow.localflow.plist runs the venv's
Python with `-m localflow.cli run` in the project folder when you log in,
and starts it again if it crashes (not when you quit it). `restart()` is the
Mac's `schtasks /run /tn "LocalFlow resume"`.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "com.goldenflow.localflow"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(project_root: Path) -> str:
    python = project_root / ".venv" / "bin" / "python"
    if not python.exists():
        raise FileNotFoundError(f"No virtualenv at {python} - run `uv sync` first")
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with open(PLIST, "wb") as fh:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": [str(python), "-m", "localflow.cli", "run"],
            "WorkingDirectory": str(project_root),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},  # again after a crash, not after Quit
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",
            "StandardErrorPath": str(project_root / "launchd.log"),
            "StandardOutPath": "/dev/null",
        }, fh)
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", _domain(), str(PLIST)], check=True)
    return str(PLIST)


def remove() -> bool:
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    if PLIST.exists():
        PLIST.unlink()
        return True
    return False


def restart() -> None:
    """Stop the running copy and start it again (as at login)."""
    subprocess.run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"], check=True)
