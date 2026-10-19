"""Start/stop cues, so you hear that recording began without looking.

Every built-in sound is synthesised here with numpy - nothing to download, and
each is short (30-200 ms) with a fade so none of them click. A setting can also
be a path to your own .wav file.

Playback uses winsound on a throwaway thread. winsound refuses SND_ASYNC with
SND_MEMORY, so the thread plays synchronously instead; a newer cue simply cuts
off an older one, which is what you want for rapid start/stop.
"""

from __future__ import annotations

import io
import logging
import sys
import threading
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .config import SoundsConfig
from .engine import Engine, State

log = logging.getLogger(__name__)

RATE = 44100


def _t(seconds: float) -> np.ndarray:
    return np.arange(int(RATE * seconds)) / RATE


def _env(n: int, attack: float = 0.003, decay: float = 30.0) -> np.ndarray:
    """Fast linear attack, exponential decay. ``decay`` is per second."""
    t = np.arange(n) / RATE
    a = np.clip(t / attack, 0, 1) if attack else 1.0
    return a * np.exp(-decay * t)


def _tone(freq: float, seconds: float, decay: float = 30.0, attack: float = 0.003) -> np.ndarray:
    t = _t(seconds)
    return np.sin(2 * np.pi * freq * t) * _env(len(t), attack, decay)


def _tick() -> np.ndarray:
    # A crisp, very short click with a little body: what most dictation apps use.
    t = _t(0.03)
    return (0.7 * np.sin(2 * np.pi * 2600 * t) + 0.3 * np.sin(2 * np.pi * 5200 * t)) * _env(len(t), 0.0005, 180)


def _pop() -> np.ndarray:
    # Bubble pop: pitch sweeps down fast.
    t = _t(0.07)
    freq = 300 + 700 * np.exp(-t * 60)
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    return np.sin(phase) * _env(len(t), 0.001, 55)


def _faint_pop() -> np.ndarray:
    # The least intrusive cue: a small, low, rounded pop played quieter than the
    # rest (see QUIET). Slower attack than "pop" so there is no click at onset.
    t = _t(0.045)
    freq = 330 + 260 * np.exp(-t * 70)
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    return np.sin(phase) * _env(len(t), 0.004, 75)


def _blip() -> np.ndarray:
    return _tone(1320, 0.08, decay=40, attack=0.004)


def _click() -> np.ndarray:
    # Mechanical switch: two tiny filtered-noise transients.
    rng = np.random.default_rng(7)
    one = rng.standard_normal(int(RATE * 0.006)) * _env(int(RATE * 0.006), 0.0002, 900)
    one = np.convolve(one, np.ones(4) / 4, mode="same")
    gap = np.zeros(int(RATE * 0.025))
    return np.concatenate([one, gap, 0.6 * one])


def _triple_click() -> np.ndarray:
    # "Are you sure?": three crisp clicks, evenly spaced.
    rng = np.random.default_rng(11)
    n = int(RATE * 0.006)
    one = np.convolve(rng.standard_normal(n) * _env(n, 0.0002, 900), np.ones(4) / 4, mode="same")
    gap = np.zeros(int(RATE * 0.11))
    return np.concatenate([one, gap, one, gap, one])


def _chime(freqs: list[float]) -> np.ndarray:
    notes = []
    for f in freqs:
        n = _tone(f, 0.16, decay=18, attack=0.004) + 0.25 * _tone(f * 2, 0.16, decay=30, attack=0.004)
        notes.append(n[: int(RATE * 0.085)])
    tail = _tone(freqs[-1], 0.18, decay=16) + 0.25 * _tone(freqs[-1] * 2, 0.18, decay=28)
    return np.concatenate(notes[:-1] + [tail])


def _soft() -> np.ndarray:
    # Low, gentle thump for people who find ticks harsh.
    return _tone(440, 0.12, decay=28, attack=0.008) + 0.4 * _tone(220, 0.12, decay=28, attack=0.008)


#: Name -> generator, in menu order. "none" is handled separately.
BUILT_IN: dict[str, Callable[[], np.ndarray]] = {
    "tick": _tick,
    "pop": _pop,
    "faint pop": _faint_pop,
    "blip": _blip,
    "click": _click,
    "triple click": _triple_click,
    "chime up": lambda: _chime([659.25, 987.77]),   # E5 -> B5
    "chime down": lambda: _chime([987.77, 659.25]),  # B5 -> E5
    "soft": _soft,
}
CHOICES = [*BUILT_IN, "none"]
#: Played at this fraction of the configured volume - meant to be barely there.
QUIET = {"faint pop": 0.45}


def render(choice: str, volume: float) -> bytes | None:
    """WAV bytes for a built-in name or a .wav path; None for "none" or unusable."""
    choice = (choice or "none").strip()
    if choice.lower() == "none":
        return None
    gen = BUILT_IN.get(choice.lower())
    if gen is None:
        path = Path(choice)
        if path.suffix.lower() == ".wav" and path.is_file():
            # Custom files play as-is; volume applies to built-ins only.
            return path.read_bytes()
        log.warning("unknown sound %r; expected one of %s or a .wav path", choice, ", ".join(CHOICES))
        return None

    samples = gen()
    fade = min(len(samples), int(RATE * 0.005))  # end at exactly zero: no click
    samples[-fade:] *= np.linspace(1.0, 0.0, fade)
    peak = float(np.abs(samples).max()) or 1.0
    level = 0.6 * QUIET.get(choice.lower(), 1.0) * max(0.0, min(volume, 1.0))
    pcm = (samples / peak * level * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


#: Generous upper bound on PlaySound start-up delay, measured 60-90 ms.
PLAY_LATENCY = 0.12


def _duration(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        return 0.3  # unreadable custom file: assume a short cue


def _play_mac(data: bytes) -> None:
    """The WAV bytes through sounddevice (macOS has no winsound)."""
    import io
    import wave

    import sounddevice as sd

    with wave.open(io.BytesIO(data)) as w:
        frames = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        frames = frames.reshape(-1, w.getnchannels())
        rate = w.getframerate()
    sd.play(frames, rate)
    sd.wait()


def play(data: bytes | None) -> None:
    if not data:
        return

    def run() -> None:
        try:
            if sys.platform == "darwin":
                _play_mac(data)
                return
            import winsound

            winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except Exception as exc:
            # Was debug-only, so "I heard no sounds" left nothing to go on.
            log.warning("sound didn't play: %s", exc)

    threading.Thread(target=run, daemon=True, name="localflow-sound").start()


class SoundCues:
    """Start cue when recording begins, stop cue when it ends, paste cue once the
    text has actually been inserted."""

    def __init__(self, engine: Engine, cfg: SoundsConfig):
        self.engine = engine
        self.cfg = cfg
        self._cache: dict[tuple[str, float], bytes | None] = {}
        self._prev = State.IDLE
        engine.on_state(self._on_state)

    def sound(self, choice: str) -> bytes | None:
        key = (choice, self.cfg.volume)
        if key not in self._cache:
            self._cache[key] = render(choice, self.cfg.volume)
        return self._cache[key]

    def _on_state(self, state: State, detail: str) -> None:
        prev, self._prev = self._prev, state
        if not self.cfg.enabled:
            return
        log.debug("cue for %s -> %s", prev.value, state.value)
        if prev == State.INJECTING and state == State.IDLE:
            # IDLE straight after INJECTING means the paste succeeded; a failed
            # paste goes to ERROR instead, so this cue only ever means "delivered".
            play(self.sound(self.cfg.paste))
            return
        if state == State.COMMAND and prev not in (State.COMMAND, State.RECORDING):
            play(self.sound(self.cfg.start))  # command mode is listening
            return
        if prev == State.COMMAND and state == State.IDLE:
            play(self.sound(self.cfg.paste))  # and has stopped
            return
        if state == State.RECORDING and prev != State.RECORDING:
            # Fired once the stream is open, so the cue means "the mic is live".
            data = self.sound(self.cfg.start)
            if data:
                # Keep the cue out of the transcript: its length plus PlaySound's
                # startup latency (measured ~60-90 ms).
                self.engine.recorder.mute_head(_duration(data) + PLAY_LATENCY)
            play(data)
        elif prev == State.RECORDING and state != State.RECORDING:
            # Cancel (Esc) stays silent - nothing is being transcribed.
            if not (state == State.IDLE and detail == "cancelled"):
                play(self.sound(self.cfg.stop))
