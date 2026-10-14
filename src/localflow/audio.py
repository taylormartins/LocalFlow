"""Microphone capture.

Records at whatever rate the interface natively runs (the Scarlett Solo will not
open a 16 kHz stream in WASAPI shared mode) and resamples to Whisper's 16 kHz
with soxr on the way out.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
import soxr

from .config import AudioConfig

log = logging.getLogger(__name__)

TARGET_RATE = 16000

#: Below this peak the capture is silence, not quiet speech.
SILENCE_PEAK = 0.005
#: Below this, speech is present but Whisper's VAD is likely to discard it.
QUIET_PEAK = 0.05

#: Fixed mic boosts, picked from the tray. 6 dB steps: each doubles the level.
BOOSTS = {"none": 1.0, "low": 2.0, "medium": 4.0, "high": 8.0}
BOOST_CHOICES = ("none", "low", "medium", "high", "auto")
#: Auto boost lifts the loudest speech in a recording to about this peak.
#: Whisper doesn't care about level, but its VAD drops quiet speech outright.
AUTO_TARGET = 0.5
#: +26 dB. More than this mostly amplifies the room.
AUTO_MAX = 20.0
#: Below this the recording is silence, and boosting it would turn room noise
#: into something Whisper hallucinates words from. Left alone instead.
AUTO_FLOOR = 0.002


def speech_level(audio: np.ndarray) -> float:
    """Robust peak: the 99.9th percentile, so one click doesn't set the level."""
    if audio.size == 0:
        return 0.0
    sample = np.abs(audio[:: max(1, audio.size // 400_000)])
    return float(np.percentile(sample, 99.9))


#: Auto never lifts the room's background (RMS of the quietest moments) above
#: this, about -40 dBFS. Level alone can't tell a quiet voice from a noisy
#: room: a click in an empty room on a normal mic peaks where a weak mic's
#: speech does, and boosting it x20 hands Whisper noise to invent words from.
AUTO_NOISE_CEIL = 0.01
#: Speech peaks rise far above the background between words. Steady noise
#: peaks at only ~3.3x its own RMS (Gaussian, 99.9th percentile), so a take
#: whose peaks sit within 6x (15.6 dB) of its quietest stretch is just noise.
AUTO_MIN_DYNAMICS = 6.0


def auto_gain(audio: np.ndarray) -> float:
    """Gain that brings ``audio``'s speech up to AUTO_TARGET. Only ever boosts."""
    level = speech_level(audio)
    if level < AUTO_FLOOR:
        return 1.0
    n = audio.size // 1024 * 1024
    if n == 0:
        return 1.0
    frames = audio[:n].reshape(-1, 1024).astype(np.float64)
    rms = np.sqrt((frames**2).mean(axis=1))
    floor = max(float(np.percentile(rms, 10)), 1e-6)
    if level / floor < AUTO_MIN_DYNAMICS:
        return 1.0
    gain = min(AUTO_TARGET / level, AUTO_MAX, AUTO_NOISE_CEIL / floor)
    return float(max(1.0, gain))


def limit(audio: np.ndarray) -> np.ndarray:
    """Soft limiter: linear up to the knee, then eases into +-1 instead of
    hard-clipping, which would add harsh distortion on boosted plosives."""
    knee = 0.8
    mag = np.abs(audio)
    over = mag > knee
    if not over.any():
        return audio
    out = audio.copy()
    squashed = knee + (1 - knee) * np.tanh((mag[over] - knee) / (1 - knee))
    out[over] = np.sign(audio[over]) * squashed
    return out


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    hostapi: str
    channels: int
    default_samplerate: float

    def __str__(self) -> str:
        return f"[{self.index}] {self.name} ({self.hostapi}, {self.channels}ch @ {self.default_samplerate:.0f}Hz)"


def list_input_devices() -> list[DeviceInfo]:
    hostapis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] < 1:
            continue
        out.append(
            DeviceInfo(
                index=i,
                name=d["name"],
                hostapi=hostapis[d["hostapi"]]["name"],
                channels=d["max_input_channels"],
                default_samplerate=d["default_samplerate"],
            )
        )
    return out


#: Preference order when the same hardware is exposed by several host APIs.
#: WASAPI has the lowest latency; WDM-KS is exclusive-mode and fails if anything
#: else holds the device, so it goes last.
API_RANK = {"Windows WASAPI": 0, "Windows DirectSound": 1, "MME": 2, "Windows WDM-KS": 4}


def resolve_candidates(spec: str) -> list[DeviceInfo]:
    """All input devices matching ``spec``, best host API first.

    Returning every match rather than one lets the recorder fall back to another
    host API when the preferred one transiently refuses to open.
    """
    devices = list_input_devices()
    if not devices:
        raise AudioError("no input devices found")

    if not spec:
        try:
            default_index = sd.default.device[0]
        except (TypeError, IndexError):
            default_index = None
        return [d for d in devices if d.index == default_index]

    needle = spec.strip().lower()
    matches = [d for d in devices if needle in d.name.lower()]
    if not matches:
        raise AudioError(
            f"no input device matching {spec!r}. Available:\n  "
            + "\n  ".join(str(d) for d in devices)
        )

    matches.sort(key=lambda d: (API_RANK.get(d.hostapi, 3), len(d.name)))
    return matches


def resolve_device(spec: str) -> DeviceInfo | None:
    """The single best input device for ``spec``, or None to use the system default."""
    candidates = resolve_candidates(spec)
    return candidates[0] if candidates else None


class Recorder:
    """Push-to-talk capture. ``start()`` then ``stop()`` returns mono float32 @ 16 kHz."""

    def __init__(self, cfg: AudioConfig):
        self.cfg = cfg
        self._stream: sd.InputStream | None = None
        self._chunks: queue.Queue[np.ndarray] = queue.Queue()
        self._lock = threading.Lock()
        self._rate = TARGET_RATE
        self._frames = 0
        self._max_frames = 0
        self._overflowed = False
        self.device: DeviceInfo | None = None
        #: (hostapi, name) pairs that opened but produced no audio this session.
        self._blacklist: set[tuple[str, str]] = set()
        self.last_peak = 0.0
        self.last_rms = 0.0
        #: Seconds at the start of the current recording to silence. Set by the
        #: start cue so its own sound, picked up by the mic, never reaches
        #: Whisper: a "pop" followed by silence transcribed as "1. 2. 1. 2. 3..."
        self._mute_head = 0.0
        self._live: list[np.ndarray] = []
        #: RMS of the most recent block (mic channel, gain applied), for live
        #: metering. A plain float write, so readers on other threads need no lock.
        self.level = 0.0
        #: Auto boost remembered from earlier recordings (smoothed), for the
        #: live meter. The final recording is always measured afresh.
        self.auto_gain = 1.0
        #: Total gain applied to the last recording, for the log and `check`.
        self.last_gain = 1.0

    @property
    def boost(self) -> str:
        b = (self.cfg.boost or "none").lower()
        return b if b in BOOST_CHOICES else "none"

    def live_gain(self) -> float:
        """Gain for metering mid-recording, before the full take exists."""
        if self.boost == "auto":
            return self.cfg.gain * self.auto_gain
        return self.cfg.gain * BOOSTS[self.boost]

    def apply_gain(self, audio: np.ndarray) -> tuple[np.ndarray, float]:
        """audio.gain, then the boost (fixed, or measured from ``audio``)."""
        gain = self.cfg.gain
        if self.boost == "auto":
            gain *= auto_gain(audio * self.cfg.gain if self.cfg.gain != 1.0 else audio)
        else:
            gain *= BOOSTS[self.boost]
        if gain == 1.0:
            return audio, gain
        return limit((audio * gain).astype(np.float32)), gain

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, frames, time_info, status):  # noqa: ANN001 - sd API
        if status:
            log.debug("audio status: %s", status)
        if self._frames >= self._max_frames:
            self._overflowed = True
            return
        block = indata.copy()
        self._chunks.put(block)
        self._live.append(block)  # same array, no extra copy; read by snapshot()
        self._frames += frames
        mic = indata[:, 0]
        self.level = float(np.sqrt(np.dot(mic, mic) / max(len(mic), 1))) * self.live_gain()

    def _open(self, device: DeviceInfo | None) -> sd.InputStream:
        index = device.index if device else None
        rate = int(device.default_samplerate) if device else TARGET_RATE
        channels = min(self.cfg.channels, device.channels) if device else 1

        stream = sd.InputStream(
            device=index,
            channels=channels,
            samplerate=rate,
            dtype="float32",
            blocksize=0,
            callback=self._callback,
        )
        stream.start()
        self._rate = rate
        return stream

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return

            candidates = resolve_candidates(self.cfg.device) or [None]
            usable = [
                d for d in candidates if d is None or (d.hostapi, d.name) not in self._blacklist
            ]
            # If every host API has misbehaved, retry them all rather than refuse
            # to record - the endpoint may have recovered.
            candidates = usable or candidates

            self._chunks = queue.Queue()
            self._live: list[np.ndarray] = []
            self._mute_head = 0.0
            self._frames = 0
            self._overflowed = False

            # A device can briefly refuse to open if another process is still
            # releasing it, so retry the preferred host API before moving on to
            # the next one rather than failing the whole dictation.
            errors: list[str] = []
            for device in candidates:
                for attempt in range(2):
                    try:
                        self._stream = self._open(device)
                        self.device = device
                        self._max_frames = int(self._rate * self.cfg.max_seconds)
                        log.info("recording from %s", device or "system default")
                        return
                    except Exception as exc:  # sd raises PortAudioError subclasses
                        self._stream = None
                        if attempt == 0:
                            time.sleep(0.15)
                            continue
                        errors.append(f"{device or 'default'}: {exc}")

            raise AudioError("could not open any matching input device:\n  " + "\n  ".join(errors))

    def snapshot(self, max_seconds: float) -> np.ndarray:
        """The last ``max_seconds`` captured so far, mono float32 @ 16 kHz,
        without stopping. For the live preview; the final transcript still
        comes from stop()."""
        blocks = list(self._live)  # list() of a list is atomic under the GIL
        rate = self._rate
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        need = int(max_seconds * rate)
        taken, total = [], 0
        for b in reversed(blocks):
            taken.append(b)
            total += len(b)
            if total >= need:
                break
        audio = np.concatenate(taken[::-1], axis=0)
        if audio.ndim > 1:
            audio = audio[:, 0]
        audio = audio[-need:].astype(np.float32)
        head = int(self._mute_head * rate) - (self._frames_in(blocks) - len(audio))
        if head > 0:
            audio[:head] = 0.0
        audio, _ = self.apply_gain(audio)
        if rate != TARGET_RATE:
            audio = soxr.resample(audio, rate, TARGET_RATE, quality="LQ")
        return np.ascontiguousarray(audio, dtype=np.float32)

    @staticmethod
    def _frames_in(blocks: list[np.ndarray]) -> int:
        return sum(len(b) for b in blocks)

    def mute_head(self, seconds: float) -> None:
        """Silence the first ``seconds`` of this recording (see _mute_head)."""
        self._mute_head = max(self._mute_head, seconds)

    def stop(self) -> np.ndarray:
        """Stop capture and return the recording. Empty array if nothing usable."""
        with self._lock:
            stream, self._stream = self._stream, None
            self.level = 0.0
            if stream is None:
                return np.zeros(0, dtype=np.float32)
            try:
                stream.stop()
            finally:
                stream.close()

            blocks = []
            while not self._chunks.empty():
                blocks.append(self._chunks.get_nowait())

        if not blocks:
            # The stream opened but the callback never fired. Seen on MME when
            # the endpoint is wedged; the host API is unusable until re-opened,
            # so drop it for the rest of the session.
            if self.device is not None:
                self._blacklist.add((self.device.hostapi, self.device.name))
                log.error(
                    "%s delivered no audio at all - dropping that host API for this session",
                    self.device.hostapi,
                )
            return np.zeros(0, dtype=np.float32)

        audio = np.concatenate(blocks, axis=0)
        if audio.ndim > 1:
            # Mixing both Scarlett inputs would fold in the unused instrument
            # channel's noise floor, so take the mic channel only.
            audio = audio[:, 0]
        audio = audio.astype(np.float32, copy=False)
        if self._mute_head > 0:
            # Zeroed rather than dropped: the start cue overlaps the moment the
            # user begins, so keep timing intact and let Whisper's VAD skip it.
            audio = audio.copy()
            audio[: int(self._mute_head * self._rate)] = 0.0

        if self._overflowed:
            log.warning("recording hit the %ss cap and was truncated", self.cfg.max_seconds)

        duration = len(audio) / self._rate
        if duration < self.cfg.min_seconds:
            log.info("discarding %.2fs recording (below min_seconds)", duration)
            return np.zeros(0, dtype=np.float32)

        raw_peak = float(np.abs(audio).max())
        audio, gain = self.apply_gain(audio)
        self.last_gain = gain
        if self.boost == "auto" and raw_peak * self.cfg.gain >= AUTO_FLOOR:
            # Geometric mean: the meter follows a changed mic within a couple of
            # recordings without jumping on one odd take.
            self.auto_gain = float(np.sqrt(self.auto_gain * gain / self.cfg.gain))

        # Always record the signal level. Without it a silent microphone and a
        # working one produce identical logs, which is exactly the failure that
        # is hardest to diagnose after the fact.
        self.last_peak = float(np.abs(audio).max())
        self.last_rms = float(np.sqrt((audio.astype(np.float64) ** 2).mean()))
        log.info(
            "captured %.1fs from %s (peak %.4f, rms %.5f; mic peak %.4f, gain x%.1f %s)",
            duration,
            self.device.hostapi if self.device else "default",
            self.last_peak,
            self.last_rms,
            raw_peak,
            gain,
            self.boost,
        )
        if self.last_peak < SILENCE_PEAK:
            log.error(
                "input is effectively silent (peak %.4f). Check the mic gain knob, "
                "48V phantom power if the mic needs it, and Windows mic privacy settings.",
                self.last_peak,
            )
        elif self.last_peak < QUIET_PEAK:
            log.warning(
                "input is very quiet (peak %.4f); Whisper's VAD may discard it. "
                "Turn up the mic gain, or set Mic boost in the tray menu (Auto fits most mics).",
                self.last_peak,
            )

        if self._rate != TARGET_RATE:
            audio = soxr.resample(audio, self._rate, TARGET_RATE, quality="HQ")

        return np.ascontiguousarray(audio, dtype=np.float32)

    def cancel(self) -> None:
        self.stop()


def decode_upload(data: bytes) -> np.ndarray:
    """Decode a browser recording (webm/opus, mp4/aac, wav) to mono float32 @ 16 kHz.

    Uses PyAV, which faster-whisper already pulls in, so phone clients can send
    whatever container their platform produces.
    """
    import io

    import av

    with av.open(io.BytesIO(data)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise AudioError("upload contains no audio stream")

        resampler = av.AudioResampler(format="flt", layout="mono", rate=TARGET_RATE)
        blocks: list[np.ndarray] = []
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                blocks.append(resampled.to_ndarray().reshape(-1))
        for resampled in resampler.resample(None):  # flush
            blocks.append(resampled.to_ndarray().reshape(-1))

    if not blocks:
        return np.zeros(0, dtype=np.float32)
    return np.ascontiguousarray(np.concatenate(blocks), dtype=np.float32)
