"""The dictation pipeline: audio -> Whisper -> cleanup -> text.

Shared by both front-ends. The tray app drives it with a hotkey and injects the
result into the focused window; the HTTP server feeds it uploaded audio from the
phone and returns the text instead.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from .audio import QUIET_PEAK, SILENCE_PEAK, Recorder, decode_upload
from .cleanup import Cleaner, apply_replacements
from .config import Config
from .inject import Injector
from .style import Stylizer, describe
from .transcribe import Transcriber

log = logging.getLogger(__name__)


class State(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    CLEANING = "cleaning"
    STYLING = "styling"
    INJECTING = "injecting"
    ERROR = "error"


@dataclass
class Result:
    text: str
    raw: str
    audio_seconds: float
    transcribe_seconds: float
    cleanup_seconds: float
    source: str = "desktop"
    at: float = field(default_factory=time.time)

    @property
    def total_seconds(self) -> float:
        return self.transcribe_seconds + self.cleanup_seconds

    @property
    def speedup(self) -> float:
        return self.audio_seconds / self.total_seconds if self.total_seconds else 0.0


StateHook = Callable[[State, str], None]
ResultHook = Callable[[Result], None]


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.recorder = Recorder(cfg.audio)
        self.transcriber = Transcriber(cfg.whisper, cfg.vocabulary)
        self.cleaner = Cleaner(cfg.cleanup, cfg.vocabulary)
        self.stylizer = Stylizer(cfg.cleanup, cfg.style)
        self.injector = Injector(cfg.inject)

        self.state = State.IDLE
        self.history: deque[Result] = deque(maxlen=50)
        self._state_hooks: list[StateHook] = []
        self._result_hooks: list[ResultHook] = []
        # Whisper and the recorder are not reentrant; one dictation at a time.
        self._pipeline_lock = threading.Lock()
        self._cleanup_enabled_for_run = True

    # -- observers ---------------------------------------------------------

    def on_state(self, hook: StateHook) -> None:
        self._state_hooks.append(hook)

    def on_result(self, hook: ResultHook) -> None:
        self._result_hooks.append(hook)

    def _set_state(self, state: State, detail: str = "") -> None:
        self.state = state
        for hook in self._state_hooks:
            try:
                hook(state, detail)
            except Exception:
                log.exception("state hook failed")

    def _emit(self, result: Result) -> None:
        self.history.append(result)
        for hook in self._result_hooks:
            try:
                hook(result)
            except Exception:
                log.exception("result hook failed")

    # -- lifecycle ---------------------------------------------------------

    def warmup(self) -> None:
        self._set_state(State.LOADING, "loading Whisper")
        try:
            self.transcriber.warmup()
            if self.cfg.cleanup.enabled and self.cleaner.uses_llm:
                self._set_state(State.LOADING, "loading cleanup model")
                self.cleaner.warmup()
            self._set_state(State.IDLE, f"ready ({self.transcriber.device_in_use})")
        except Exception as exc:
            log.exception("warmup failed")
            self._set_state(State.ERROR, str(exc))

    def shutdown(self) -> None:
        self.recorder.cancel()
        self.cleaner.close()
        self.stylizer.close()
        self.transcriber.unload()

    # -- push-to-talk ------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self.recorder.recording

    @property
    def cleanup_this_run(self) -> bool:
        """Whether the current/next dictation will even try the LLM (not raw mode,
        not switched off in the tray)."""
        return self._cleanup_enabled_for_run and self.cfg.cleanup.enabled

    def start_recording(self, cleanup: bool = True) -> None:
        if self.recorder.recording:
            return
        self._cleanup_enabled_for_run = cleanup
        try:
            self.recorder.start()
        except Exception as exc:
            log.error("%s", exc)
            self._set_state(State.ERROR, str(exc))
            return
        self._set_state(State.RECORDING, "listening" if cleanup else "listening (raw)")

    def cancel_recording(self) -> None:
        if not self.recorder.recording:
            return
        self.recorder.cancel()
        self._set_state(State.IDLE, "cancelled")

    def stop_recording(self, inject: bool = True) -> None:
        """Stop capture and process in the background so the hotkey thread stays free."""
        if not self.recorder.recording:
            return
        audio = self.recorder.stop()
        if audio.size == 0:
            self._set_state(State.ERROR, "no audio captured - check the mic")
            return
        threading.Thread(
            target=self._process_and_inject,
            args=(audio, self._cleanup_enabled_for_run, inject),
            daemon=True,
            name="localflow-pipeline",
        ).start()

    def _process_and_inject(self, audio: np.ndarray, cleanup: bool, inject: bool) -> None:
        try:
            result = self.process(audio, cleanup=cleanup, source="desktop")
        except Exception as exc:
            log.exception("pipeline failed")
            self._set_state(State.ERROR, str(exc))
            return

        if not result.text:
            # Distinguish "your mic is dead" from "you didn't say anything",
            # because they look identical from the outside and need different fixes.
            peak = self.recorder.last_peak
            if peak < SILENCE_PEAK:
                detail = f"no signal from the mic (peak {peak:.4f}) - check gain/48V"
            elif peak < QUIET_PEAK:
                detail = f"input too quiet (peak {peak:.4f}) - turn up the mic gain"
            else:
                detail = "no speech detected"
            self._set_state(State.ERROR if peak < QUIET_PEAK else State.IDLE, detail)
            log.warning("empty transcript: %s", detail)
            return

        if inject:
            self._set_state(State.INJECTING, result.text[:60])
            try:
                self.injector.send(result.text)
            except Exception as exc:
                log.error("injection failed: %s", exc)
                self._set_state(State.ERROR, f"could not insert text: {exc}")
                return

        self._set_state(
            State.IDLE,
            f"{len(result.text)} chars in {result.total_seconds:.1f}s ({result.speedup:.0f}x)",
        )

    def preview(self, audio: np.ndarray) -> str | None:
        """Transcribe for the live overlay, or None if the pipeline is busy.

        Whisper is not reentrant, so this shares the pipeline lock - but never
        waits on it: a skipped preview frame is better than delaying a real
        dictation that is finishing in the background.
        """
        if not self._pipeline_lock.acquire(blocking=False):
            return None
        try:
            return self.transcriber.transcribe(audio)
        finally:
            self._pipeline_lock.release()

    # -- core pipeline -----------------------------------------------------

    def process(self, audio: np.ndarray, cleanup: bool = True, source: str = "desktop") -> Result:
        """Transcribe and clean an audio buffer. Blocking; safe from any thread."""
        with self._pipeline_lock:
            duration = len(audio) / 16000

            self._set_state(State.TRANSCRIBING, f"{duration:.1f}s")
            t0 = time.perf_counter()
            raw = self.transcriber.transcribe(audio)
            transcribe_s = time.perf_counter() - t0

            text = raw
            cleanup_s = 0.0
            self.cleaner.last_outcome = ""
            # A style on its own model does cleanup + style in one pass (see
            # Stylizer.separate_model), so the cleanup model is never loaded.
            # With algorithmic cleanup there is no cleanup model: rules, then style.
            one_pass = (
                cleanup and self.stylizer.active and self.stylizer.separate_model
                and self.cleaner.uses_llm
            )
            if raw and cleanup and self.cfg.cleanup.enabled and not one_pass:
                self._set_state(State.CLEANING, "")
                t1 = time.perf_counter()
                text = self.cleaner.clean(raw)
                cleanup_s = time.perf_counter() - t1

            self.stylizer.last_outcome = ""
            # Character/tone rewrite. Skipped in raw mode (Shift+Caps): raw means verbatim.
            if text and cleanup and self.stylizer.active:
                self._set_state(State.STYLING, describe(self.cfg.style))
                t2 = time.perf_counter()
                text = self.stylizer.apply(text, also_clean=one_pass)
                if one_pass and self.stylizer.last_outcome in ("rejected", "failed"):
                    # Fall back to ordinary cleanup so the text is still tidy.
                    # Costs one model swap, but only when the style pass failed.
                    self._set_state(State.CLEANING, "")
                    text = self.cleaner.clean(raw) if self.cfg.cleanup.enabled else raw
                cleanup_s += time.perf_counter() - t2

            if text and self.cfg.vocabulary.replacements:
                text = apply_replacements(text, self.cfg.vocabulary.replacements)

            result = Result(
                text=text.strip(),
                raw=raw,
                audio_seconds=duration,
                transcribe_seconds=transcribe_s,
                cleanup_seconds=cleanup_s,
                source=source,
            )
            log.info(
                "%s: %.1fs audio -> %d chars (whisper %.2fs, cleanup %.2fs)",
                source,
                duration,
                len(result.text),
                transcribe_s,
                cleanup_s,
            )
            self._emit(result)
            return result

    def process_upload(self, data: bytes, cleanup: bool = True, source: str = "phone") -> Result:
        """Same pipeline, fed by an encoded audio file from a remote client."""
        audio = decode_upload(data)
        try:
            return self.process(audio, cleanup=cleanup, source=source)
        finally:
            self._set_state(State.IDLE, "")
