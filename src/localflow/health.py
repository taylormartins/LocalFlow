"""Is the cleanup LLM (Ollama) answering? Watched, shown in the tray, restartable.

A dead Ollama used to be silent: every dictation fell back to the rules and
nothing said why. Worse, a hung one (answering /api/tags but not chats) cost
the full cleanup timeout on every dictation. The watcher probes it, tells you
once when it goes and once when it's back, and while it's down the cleaner
skips the LLM straight away.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

#: Seconds between probes while it's fine, and while it isn't.
EVERY_OK = 120.0
EVERY_BAD = 20.0
#: A one-token reply has to come back within this. Long enough to load the
#: model from a slow drive first (~40 s measured), so loading isn't "hung".
PROBE_TIMEOUT = 90.0

OK, DOWN, MISSING, HUNG = "ok", "down", "missing", "hung"
LABELS = {
    OK: "answering",
    DOWN: "not running",
    MISSING: "model not found",
    HUNG: "not answering",
}


class OllamaHealth:
    """Probes Ollama in the background while the cleanup uses it."""

    def __init__(self, cleaner, on_change: Callable[[str, str], None] | None = None):
        self.cleaner = cleaner
        self.on_change = on_change  # (status, message)
        self.status = OK  # assume fine until a probe says otherwise
        self.restarting = False
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        cleaner.on_failure = self.suspect

    @property
    def ok(self) -> bool:
        return self.status == OK

    def label(self) -> str:
        if self.restarting:
            return "Ollama: restarting..."
        return f"Ollama: {LABELS[self.status]}"

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="localflow-ollama-health")
            self._thread.start()

    def suspect(self) -> None:
        """A cleanup or command call just failed: probe now."""
        self._wake.set()

    def _loop(self) -> None:
        while True:
            woken = self._wake.wait(EVERY_OK if self.ok else EVERY_BAD)
            self._wake.clear()
            if self.cleaner.uses_llm and not self.restarting:
                # The routine check while all is well only asks if it's up; a
                # failure (or being down already) gets the full test.
                self.check(full=woken or not self.ok)

    def probe(self, full: bool = True) -> str:
        """OK, DOWN (no server), MISSING (model not installed) or HUNG.
        ``full`` also has it generate one token."""
        c = self.cleaner
        try:
            resp = c._client.get("/api/tags", timeout=3.0)
            resp.raise_for_status()
            names = {m.get("name") for m in resp.json().get("models", [])}
        except Exception:
            return DOWN
        if c.cfg.model not in names and f"{c.cfg.model}:latest" not in names:
            return MISSING
        if not full:
            return OK
        try:
            # One token: proves it actually generates, not just that it's up.
            c._client.post("/api/generate", timeout=PROBE_TIMEOUT, json={
                "model": c.cfg.model, "prompt": "ok", "stream": False, "think": False,
                "keep_alive": c.cfg.keep_alive, "options": {"num_predict": 1},
            }).raise_for_status()
        except Exception as exc:
            log.debug("ollama probe: %s", exc)
            return HUNG
        return OK

    def check(self, full: bool = True) -> str:
        status = self.probe(full)
        self._set(status)
        return status

    def _set(self, status: str) -> None:
        if status == self.status:
            return
        before, self.status = self.status, status
        self.cleaner.llm_down = status != OK
        model = self.cleaner.cfg.model
        if status == OK:
            msg = "Ollama is answering again: LLM cleanup is back"
        elif status == MISSING:
            msg = f"Ollama can't find {model} (is its models drive connected?) - using algorithmic cleanup"
        elif status == DOWN:
            msg = "Ollama isn't running - using algorithmic cleanup. Tray > Restart Ollama"
        else:
            msg = "Ollama isn't answering - using algorithmic cleanup. Tray > Restart Ollama"
        log.log(logging.INFO if status == OK else logging.WARNING, "ollama: %s -> %s",
                before, status)
        if self.on_change is not None:
            try:
                self.on_change(status, msg)
            except Exception:
                log.exception("ollama health hook failed")

    def restart(self) -> None:
        """Stop Ollama and start it again, then load the model. Blocking."""
        if self.restarting:
            return
        self.restarting = True
        try:
            app, server = _ollama_paths()
            for name in ("ollama app.exe", "ollama.exe"):
                subprocess.run(["taskkill", "/f", "/im", name], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(1.0)
            if app is not None:
                # Through Explorer, so it runs as you, not elevated like LocalFlow.
                subprocess.Popen(["explorer.exe", str(app)],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            elif server is not None:
                subprocess.Popen([str(server), "serve"], creationflags=subprocess.CREATE_NO_WINDOW,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                log.warning("ollama: can't find it to start it")
            log.info("ollama: restarted")
            for _ in range(30):  # the server comes up in a few seconds
                time.sleep(1.0)
                try:
                    self.cleaner._client.get("/api/tags", timeout=1.0).raise_for_status()
                    break
                except Exception:
                    pass
            self.cleaner.warmup()
        finally:
            self.restarting = False
        self.check()


def _ollama_paths() -> tuple[Path | None, Path | None]:
    """The tray app and the server, from the standard install or PATH."""
    import shutil

    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"
    app = base / "ollama app.exe"
    server = base / "ollama.exe"
    found = shutil.which("ollama")
    return (app if app.is_file() else None,
            server if server.is_file() else (Path(found) if found else None))
