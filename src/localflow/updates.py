"""Which version is running, and is there a newer one?

The newest version is the highest vX.Y.Z tag on GitHub, read with `git
ls-remote` - the same route the updater uses, so it works behind proxies
that let git through (no API calls, no extra certificates). Checked at
startup and every few hours; the tray menu shows the answer, and the
startup notice says when there's something new.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from importlib.metadata import version

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

CHECK_EVERY = 6 * 3600


def current() -> str:
    """The version of the code on disk (pyproject.toml).

    Not the installed package's metadata: that only changes when an install
    finishes, so after one that died half way it still said 1.7.0 on 1.13.0
    code, and the tray offered an update to the version already there.
    """
    try:
        import tomllib

        with (PROJECT_ROOT / "pyproject.toml").open("rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except Exception:
        return installed()


def installed() -> str:
    """The version the last finished install set up (package metadata)."""
    try:
        return version("localflow")
    except Exception:
        return "?"


def install_incomplete() -> bool:
    """New code on an old install: the dependencies may be missing or stale."""
    return installed() != current()


def parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def newest(timeout: float = 20.0) -> str | None:
    """The highest released version on GitHub, or None if it can't be told."""
    from .proc import NO_WINDOW

    try:
        out = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", "origin"], cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info("update check failed: %s", exc)
        return None
    if out.returncode:
        log.info("update check failed: git ls-remote exited %d", out.returncode)
        return None
    tags = re.findall(r"refs/tags/v(\d+\.\d+\.\d+)$", out.stdout, re.M)
    return max(tags, key=parse) if tags else None


class Checker:
    """Keeps `latest` fresh in the background; `state` is for the tray."""

    def __init__(self, on_new=None):
        self.latest: str | None = None
        self.checked_at = 0.0
        self.on_new = on_new  # called once per new version found
        self._told: str | None = None
        self._lock = threading.Lock()

    @property
    def update_available(self) -> bool:
        return bool(self.latest) and parse(self.latest) > parse(current())

    def label(self) -> str:
        now = current()
        if install_incomplete():
            return f"Version {now} - install not finished"
        if self.latest is None:
            return f"Version {now}"
        if self.update_available:
            return f"Version {now} - {self.latest} available"
        return f"Version {now} - the latest"

    def check(self) -> str | None:
        with self._lock:
            latest = newest()
            if latest:
                self.latest, self.checked_at = latest, time.time()
                log.info("version %s; newest released %s", current(), latest)
                if self.update_available and self._told != latest and self.on_new:
                    self._told = latest
                    self.on_new(latest)
            return latest

    def start(self, first_after: float = 60.0) -> None:
        def loop() -> None:
            time.sleep(first_after)  # after the models have loaded
            while True:
                self.check()
                time.sleep(CHECK_EVERY)

        threading.Thread(target=loop, daemon=True, name="localflow-update-check").start()
