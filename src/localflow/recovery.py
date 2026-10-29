"""Crash recovery for dictation: what you were saying is kept on disk.

While a recording runs, ``Recorder`` appends its audio to ``data/recovery/``
(one ``take-<date>-<time>-<rate>.pcm`` file, 16-bit mono, the rate in the
name). Once the dictation has been transcribed and is safe in Dictation
history the file is deleted. If LocalFlow dies first (a crash, a kill, a
reboot), the file is still there at the next start: ``recover_in_background``
transcribes it, puts the text in history, on the clipboard, and says so.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

import numpy as np

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

#: A take shorter than this is a key tap, not something to recover.
MIN_SECONDS = 2.0
#: Takes older than this are stale: removed without a word.
MAX_AGE = 3 * 24 * 3600
_NAME = re.compile(r"^take-\d{8}-\d{6}-(\d+)\.pcm$")


def folder() -> Path:
    return PROJECT_ROOT / "data" / "recovery"


class Spool:
    """The open file one recording writes into. Never raises: a full disk or a
    locked file must not break dictation, it only stops the safety copy."""

    def __init__(self, rate: int):
        self.path: Path | None = None
        self._fh = None
        self._last_flush = time.monotonic()
        try:
            folder().mkdir(parents=True, exist_ok=True)
            t = time.time()
            while True:  # two takes in one second must not share a file
                self.path = folder() / f"take-{time.strftime('%Y%m%d-%H%M%S', time.localtime(t))}-{int(rate)}.pcm"
                if not self.path.exists():
                    break
                t += 1
            self._fh = open(self.path, "wb")
        except OSError:
            log.warning("recovery copy unavailable", exc_info=True)
            self._fh = None

    def write(self, block: np.ndarray) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            mic = block[:, 0] if block.ndim > 1 else block
            fh.write((np.clip(mic, -1.0, 1.0) * 32767).astype("<i2").tobytes())
            now = time.monotonic()
            if now - self._last_flush >= 1.0:  # at most a second is ever lost
                fh.flush()
                self._last_flush = now
        except (OSError, ValueError):
            self._fh = None  # closed under us, or the disk is full

    def close(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


def discard(path: Path | None) -> None:
    """The dictation is safe (in history) or was cancelled: drop its copy."""
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        log.debug("could not remove %s", path, exc_info=True)


def pending() -> list[Path]:
    """Takes left behind, oldest first."""
    try:
        return sorted(p for p in folder().glob("take-*.pcm") if _NAME.match(p.name))
    except OSError:
        return []


def load(path: Path) -> tuple[np.ndarray, int]:
    """(mono float32 samples, their rate) from a take."""
    rate = int(_NAME.match(Path(path).name).group(1))
    raw = np.fromfile(path, dtype="<i2")
    return raw.astype(np.float32) / 32767.0, rate


def recover(engine, notify=None, paths: list[Path] | None = None) -> str:
    """Transcribe every take left behind; returns the text (also put in history
    by the pipeline, and on the clipboard). A take that fails is renamed
    ``.bad`` once, so it can never crash-loop the start. ``paths``: the takes
    that were there when this run began (a take being recorded now is not one)."""
    import soxr

    texts: list[str] = []
    seconds = 0.0
    for path in (pending() if paths is None else paths):
        try:
            if time.time() - path.stat().st_mtime > MAX_AGE:
                discard(path)
                continue
            audio, rate = load(path)
            if len(audio) / rate < MIN_SECONDS:
                discard(path)
                continue
            bad = path.with_suffix(".bad")
            path.replace(bad)  # out of the way until it has worked
            audio, _gain = engine.recorder.apply_gain(audio)
            if rate != 16000:
                audio = soxr.resample(audio, rate, 16000, quality="HQ")
            log.info("recovering %.0fs of dictation left by the last run (%s)", len(audio) / 16000,
                     path.name)
            result = engine.process(np.ascontiguousarray(audio, dtype=np.float32), cleanup=True,
                                    source="recovered")
            discard(bad)
            seconds += len(audio) / 16000
            if result.text:
                texts.append(result.text)
        except Exception:
            log.exception("could not recover %s", path.name)
    text = "\n\n".join(texts)
    if notify is not None and (text or seconds):
        if text:
            try:
                from .inject import set_clipboard_text

                set_clipboard_text(text)
            except Exception:
                log.debug("clipboard unavailable", exc_info=True)
            notify(f"Recovered {seconds:.0f}s of what you were saying: it's on the clipboard "
                   "and in Dictation history", "ok", 15)
        else:
            notify("Found a saved recording but there was no speech in it", "info", 8)
    return text


def recover_in_background(engine, paths: list[Path]) -> None:
    """At startup, once the models are loaded; ``paths`` from pending() taken
    before anything could start recording."""
    if not paths:
        return
    threading.Thread(target=lambda: recover(engine, engine.notice, paths), daemon=True,
                     name="localflow-recovery").start()
