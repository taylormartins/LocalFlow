"""Live transcript while you speak, for the overlay.

Every ``live_interval`` seconds during a recording, the most recent
``live_window`` seconds of audio go through Whisper and the overlay shows the
sentence you are in the middle of. Display only: the pasted text always comes
from the full transcription after you stop, so a rough preview costs nothing
in accuracy.
"""

from __future__ import annotations

import logging
import re
import threading
import time

from .config import OverlayConfig
from .engine import Engine, State

log = logging.getLogger(__name__)

#: Whisper needs a little audio before it says anything sensible.
MIN_AUDIO_SECONDS = 0.8

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def display_words(
    text: str, context_words: int, max_words: int
) -> tuple[list[str], list[str], bool]:
    """Split a running transcript into (dim context, bright current, cut-off?).

    Shows the sentence being spoken. Once a new sentence starts, the last
    ``context_words`` of the previous one stay as dimmed context. Never more than
    ``max_words`` in total: context goes first, then the start of an overly long
    sentence (the overlay marks that with "...").
    """
    sentences = [s for s in _SENTENCE_END.split(text.strip()) if s]
    if not sentences:
        return [], [], False
    current = sentences[-1].split()
    context = sentences[-2].split()[-context_words:] if len(sentences) > 1 and context_words else []

    overflow = len(context) + len(current) - max_words
    if overflow > 0:
        drop = min(overflow, len(context))
        context = context[drop:]
        overflow -= drop
    cut = overflow > 0
    if cut:
        current = current[overflow:]
    return context, current, cut


class LivePreview:
    def __init__(self, engine: Engine, cfg: OverlayConfig):
        self.engine = engine
        self.cfg = cfg
        #: Latest preview text and a counter the overlay uses to spot changes.
        self.text = ""
        self.version = 0
        self._thread: threading.Thread | None = None
        engine.on_state(self._on_state)

    def _on_state(self, state: State, detail: str) -> None:
        if state == State.RECORDING:
            self._set("")
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True, name="localflow-live")
                self._thread.start()

    def _set(self, text: str) -> None:
        self.text = text
        self.version += 1

    def _loop(self) -> None:
        recorder = self.engine.recorder
        while self.engine.recording:
            started = time.monotonic()
            if self.engine.transcriber.device_in_use == "cpu":
                # A CPU Whisper pass takes seconds and starves everything else;
                # the preview is a nicety, so stand down until it's on the GPU.
                time.sleep(self.cfg.live_interval)
                continue
            try:
                audio = recorder.snapshot(self.cfg.live_window)
                if len(audio) >= MIN_AUDIO_SECONDS * 16000 and self.engine.recording:
                    text = self.engine.preview(audio)
                    # None = pipeline busy; keep the last preview rather than blank it.
                    if text is not None and self.engine.recording:
                        self._set(text)
            except Exception:
                log.debug("live preview failed", exc_info=True)
            # Interval measured start-to-start, so slow passes don't pile up.
            time.sleep(max(0.1, self.cfg.live_interval - (time.monotonic() - started)))
