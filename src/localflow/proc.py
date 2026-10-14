"""Starting LocalFlow's own helper processes without a console window.

The venv's pythonw.exe is a launcher that starts the console python.exe, and
where Windows Terminal is the default terminal that console shows up as a
visible window (restart.ps1 has the full story). Inside the app, sys.executable
*is* that launcher, so a plain subprocess.call([sys.executable, ...]) popped a
terminal next to the setup window. So, like restart.ps1: run the base
interpreter's real pythonw.exe and tell it which venv it is through
__PYVENV_LAUNCHER__. CREATE_NO_WINDOW covers console programs (powershell,
git, uv) and anything they start in turn.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import PROJECT_ROOT

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def gui_python() -> tuple[str, dict[str, str]]:
    """(pythonw.exe to run, environment) for a windowless LocalFlow process."""
    env = dict(os.environ)
    launcher = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    base = Path(getattr(sys, "_base_executable", sys.executable)).with_name("pythonw.exe")
    if base.exists() and launcher.exists():
        env["__PYVENV_LAUNCHER__"] = str(launcher)
        return str(base), env
    return str(Path(sys.executable).with_name("pythonw.exe")), env


def run_localflow(*args: str) -> int:
    """Run `localflow <args>` windowless and wait for it. Returns the exit code."""
    exe, env = gui_python()
    return subprocess.call(
        [exe, "-m", "localflow.cli", *args], cwd=str(PROJECT_ROOT), env=env,
        creationflags=NO_WINDOW,
    )


def launch_detached_powershell(script: Path, *args: str) -> None:
    """Start a .ps1 hidden and *outside* our process tree, then return.

    The updater restarts LocalFlow, and restart.ps1 stops the old app with
    `taskkill /T`, which kills its whole process tree. A child of ours would
    die with us mid-update. Started through an intermediate powershell that
    exits at once, the updater's parent is gone and the tree kill passes it by.
    """
    # Start-Process joins -ArgumentList with spaces and quotes nothing, so the
    # script path carries its own double quotes (it may contain spaces).
    parts = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", f'"{script}"', *args]
    inner = ",".join("'" + a.replace("'", "''") + "'" for a in parts)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
         f"Start-Process powershell -WindowStyle Hidden -WorkingDirectory '{PROJECT_ROOT}' "
         f"-ArgumentList {inner}"],
        cwd=str(PROJECT_ROOT), creationflags=NO_WINDOW,
    )


def run_powershell(script: Path, *args: str) -> int:
    """Run a .ps1 hidden and wait for it. Returns the exit code."""
    return subprocess.call(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
         "-File", str(script), *args],
        cwd=str(PROJECT_ROOT), creationflags=NO_WINDOW,
    )
