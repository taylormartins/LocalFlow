"""Always-on mode: listen for the start phrase, dictate, stop on the stop phrase.

Built for the smallest footprint that still reacts quickly:

  * The mic stays open, but between phrases the only work per 30 ms frame is
    one RMS against an adaptive noise floor. Audio lives in a 30 s ring
    buffer, overwritten as it goes; nothing is kept or logged.
  * Only a burst of speech (ended by half a second of quiet, or cut every
    6 s with 2 s of overlap so a phrase can't straddle the cut) is handed to
    the detector: a small Whisper model on 2 CPU threads, behind Silero VAD,
    so noise that merely looks loud is rejected before any transcription.
  * Once the start phrase is heard, the audio from just after it is fed into
    the normal recorder, so the overlay, cues, live preview, mic boost and
    cleanup behave exactly as with a hotkey. The stop phrase's audio is cut
    off the end, and any word of either phrase that survives the cut is
    stripped from the transcript.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import numpy as np

from .audio import resolve_candidates
from .config import Config
from .wake import RATE, Detector, Hit, Phrases, strip_phrases

log = logging.getLogger(__name__)

FRAME = 480  # 30 ms
RING_SECONDS = 30
PREROLL = 0.3  # kept before the first loud frame: soft consonants start quiet
HANGOVER = 0.5  # quiet that ends a burst
MAX_BURST = 6.0
OVERLAP = 2.0
#: Margins at the cuts. Whisper's word times are good to ~0.1 s; erring into
#: the phrase is fixed by strip_phrases, erring into the dictation is not.
START_MARGIN = 0.08
STOP_MARGIN = 0.05


class Ring:
    """Last RING_SECONDS of 16 kHz audio, addressed by absolute sample index."""

    def __init__(self, seconds: float = RING_SECONDS):
        self.buf = np.zeros(int(seconds * RATE), dtype=np.float32)
        self.end = 0  # absolute index one past the newest sample

    def write(self, x: np.ndarray) -> None:
        total, size = len(x), len(self.buf)
        if total >= size:
            # Only the newest `size` samples fit; write them where they belong.
            self.end += total - size
            x = x[-size:]
        n = len(x)
        i = self.end % size
        first = min(n, size - i)
        self.buf[i : i + first] = x[:first]
        self.buf[: n - first] = x[first:]
        self.end += n

    def get(self, a: int, b: int) -> np.ndarray:
        size = len(self.buf)
        a, b = max(a, self.end - size, 0), min(b, self.end)
        if b <= a:
            return np.zeros(0, dtype=np.float32)
        idx = np.arange(a, b) % size
        return self.buf[idx].copy()


class Listener:
    def __init__(
        self, engine, cfg: Config, *, inject: bool = True,
        on_event: Callable[[str, str], None] | None = None,
    ):
        self.engine = engine
        self.cfg = cfg
        self.inject = inject
        self.on_event = on_event or (lambda kind, text: None)
        self.detector = Detector(cfg.always_on.model, Phrases.load(), cfg.always_on.accept)
        self.ring = Ring()
        self._lock = threading.Lock()  # ring writes + feeding vs. starting/stopping
        self._blocks: queue.Queue[np.ndarray | None] = queue.Queue()
        self._bursts: queue.Queue[tuple[int, int] | None] = queue.Queue(maxsize=4)
        self._stream = None
        self._threads: list[threading.Thread] = []
        self.running = False
        self.paused = False
        self.dictating = False
        self._dict_start = 0
        self._last_speech = 0
        # segmenter state
        self._carry = np.zeros(0, dtype=np.float32)
        self._floor = 0.002
        self._run = 0
        self._in_speech = False
        self._burst_start = 0
        self._quiet = 0

    # -- lifecycle -----------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self.detector.phrases.ready

    def reload(self) -> None:
        self.detector.set_phrases(Phrases.load())
        log.info("always-on: phrases %r / %r", self.detector.phrases.start.text,
                 self.detector.phrases.stop.text)

    def start(self) -> None:
        if self.running:
            return
        if not self.ready:
            raise RuntimeError("set up the start and stop phrases first")
        self.running = True
        self._threads = [
            threading.Thread(target=self._audio_loop, daemon=True, name="localflow-always-on"),
            threading.Thread(target=self._detect_loop, daemon=True, name="localflow-wake"),
        ]
        for t in self._threads:
            t.start()
        self._open_stream()
        self.engine.external_stop = self.finish
        # Load the listening model now, not on the first phrase.
        threading.Thread(target=self.detector.load, daemon=True, name="localflow-wake-load").start()
        log.info("always-on: listening for %r", self.detector.phrases.start.text)

    def _open_stream(self) -> None:
        import sounddevice as sd

        errors = []
        for device in resolve_candidates(self.cfg.audio.device) or [None]:
            rate = int(device.default_samplerate) if device else RATE
            try:
                stream = sd.InputStream(
                    device=device.index if device else None, channels=1, samplerate=rate,
                    dtype="float32", blocksize=0,
                    callback=lambda indata, *_: self._blocks.put(indata[:, 0].copy()),
                )
                stream.start()
            except Exception as exc:
                errors.append(f"{device or 'default'}: {exc}")
                continue
            self._stream, self._rate = stream, rate
            return
        self.running = False
        raise RuntimeError("always-on could not open the mic: " + "; ".join(errors))

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self.dictating:
            self.finish()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._blocks.put(None)
        try:
            self._bursts.put_nowait(None)
        except queue.Full:
            pass
        self.engine.external_stop = None
        log.info("always-on: off")

    # -- audio thread --------------------------------------------------------

    def _audio_loop(self) -> None:
        import soxr

        resampler = None
        while True:
            block = self._blocks.get()
            if block is None:
                return
            if self.paused:
                continue
            if self._rate != RATE:
                if resampler is None:
                    resampler = soxr.ResampleStream(self._rate, RATE, 1, dtype="float32")
                block = resampler.resample_chunk(block)
            self.ingest(block)

    def ingest(self, x: np.ndarray) -> None:
        """16 kHz audio in: ring buffer, dictation feed, burst detection."""
        if x.size == 0:
            return
        with self._lock:
            self.ring.write(x)
            if self.dictating:
                if self.engine.recorder.external:
                    self.engine.feed(x)
                else:
                    # Cancelled (Esc) or finished elsewhere.
                    self.dictating = False
        self._segment(x)
        if self.dictating:
            idle = (self.ring.end - self._last_speech) / RATE
            if idle > self.cfg.always_on.idle_stop_seconds or self.engine.recorder.overflowed:
                log.info("always-on: %.0fs without speech - finishing", idle)
                self.finish()

    def _segment(self, x: np.ndarray) -> None:
        x = np.concatenate([self._carry, x]) if self._carry.size else x
        n = len(x) // FRAME * FRAME
        self._carry = x[n:]
        if n == 0:
            return
        frames = x[:n].reshape(-1, FRAME)
        rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
        end = self.ring.end - len(self._carry)  # absolute end of the last frame
        first = end - n
        for k, r in enumerate(rms):
            # Noise floor: follows quiet quickly, creeps up slowly (~5 %/s) so
            # a steady fan or hum stops counting as speech after a while.
            if r < self._floor:
                self._floor = 0.9 * self._floor + 0.1 * max(r, 1e-6)
            else:
                self._floor *= 1.0015
            loud = r > max(3.0 * self._floor, 4e-4)
            t = first + (k + 1) * FRAME
            if loud:
                self._last_speech = t
            if not self._in_speech:
                self._run = self._run + 1 if loud else 0
                if self._run >= 3:
                    self._in_speech, self._quiet = True, 0
                    self._burst_start = t - 3 * FRAME - int(PREROLL * RATE)
                continue
            self._quiet = 0 if loud else self._quiet + 1
            if self._quiet * FRAME >= HANGOVER * RATE:
                self._emit(self._burst_start, t - self._quiet * FRAME + int(0.2 * RATE))
                self._in_speech, self._run = False, 0
            elif t - self._burst_start >= MAX_BURST * RATE:
                self._emit(self._burst_start, t)
                self._burst_start = t - int(OVERLAP * RATE)

    def _emit(self, a: int, b: int) -> None:
        try:
            self._bursts.put_nowait((a, b))
        except queue.Full:
            # The detector is behind (a long stretch of talk or music). Drop
            # the oldest burst: the newest is the one a phrase is likely in.
            try:
                self._bursts.get_nowait()
                self._bursts.put_nowait((a, b))
            except queue.Empty:
                pass

    # -- detector thread -----------------------------------------------------

    def _detect_loop(self) -> None:
        while True:
            item = self._bursts.get()
            if item is None:
                return
            if self.paused:
                continue
            try:
                self._check(*item)
            except Exception:
                log.exception("always-on: detection failed")

    def _check(self, a: int, b: int) -> None:
        clip = self.ring.get(a, b)
        if clip.size < 0.3 * RATE:
            return
        clip, _ = self.engine.recorder.apply_gain(clip)
        started = time.perf_counter()
        words = self.detector.words(clip)
        log.debug("always-on: %.1fs burst -> %d words in %.2fs", len(clip) / RATE,
                  len(words), time.perf_counter() - started)
        if not words:
            return
        self.on_event("heard", " ".join(w.text for w in words))

        if not self.dictating:
            if self.engine.recording or self.engine.state.value not in ("idle", "error"):
                return  # a hotkey dictation or processing is under way
            hit = self.detector.check("start", words, clip)
            if hit is None:
                return
            self._begin(a, b, hit)
            # Start and stop in one breath: check the rest of this burst too.
            stop = self.detector.check("stop", words, clip, after=hit.end)
            if stop is not None:
                self._end(a, stop)
            return

        after = max(0.0, (self._dict_start - a) / RATE)
        hit = self.detector.check("stop", words, clip, after=after)
        if hit is not None:
            self._end(a, hit)

    def _begin(self, a: int, b: int, hit: Hit) -> None:
        log.info("always-on: start phrase (%r, %.2f, %s)", hit.heard, hit.score, hit.by)
        self.on_event("start", hit.heard)
        with self._lock:
            if hit.next_start is None:
                # A pause followed the phrase, so the burst ends with it: start
                # where the burst ends rather than at Whisper's (often early)
                # end time for its last word, which left "...gold" behind.
                begin = max(a + int((hit.end + START_MARGIN) * RATE), b)
            else:
                # Talking straight on: cut just before the next word.
                at = max(hit.end + START_MARGIN, min(hit.next_start - 0.05, hit.end + 0.6))
                begin = a + int(at * RATE)
            prefill = self.ring.get(begin, self.ring.end)
            if not self.engine.start_external(prefill):
                return
            self._dict_start = begin
            self._last_speech = self.ring.end
            self.dictating = True

    def _end(self, a: int, hit: Hit) -> None:
        log.info("always-on: stop phrase (%r, %.2f, %s)", hit.heard, hit.score, hit.by)
        self.on_event("stop", hit.heard)
        at = hit.start - STOP_MARGIN
        if hit.prev_end is not None:
            # Not before the end of the last dictated word.
            at = max(at, min(hit.prev_end + 0.02, hit.start))
        stop_at = a + int(at * RATE)
        self.finish(keep=stop_at - self._dict_start)

    def finish(self, keep: int | None = None) -> None:
        """End the current dictation (stop phrase, hotkey, idle or overflow)."""
        with self._lock:
            if not self.dictating:
                return
            self.dictating = False
        start = self.detector.variants("start")
        stop = self.detector.variants("stop")
        self.engine.finish_external(
            keep, transform=lambda raw: strip_phrases(raw, start, stop), inject=self.inject
        )

    # -- testing -------------------------------------------------------------

    def run_file(self, audio: np.ndarray, chunk: float = 0.1) -> None:
        """Push a 16 kHz recording through as if it came from the mic, in real
        order but without real time, waiting for detection after each chunk so
        timing-dependent behaviour matches live use."""
        self.running = True
        self.engine.external_stop = self.finish
        step = int(chunk * RATE)
        for i in range(0, len(audio), step):
            self.ingest(audio[i : i + step])
            while not self._bursts.empty():
                self._check(*self._bursts.get_nowait())
        # Trailing quiet so a final burst ends.
        self.ingest(np.zeros(int(1.0 * RATE), dtype=np.float32))
        while not self._bursts.empty():
            self._check(*self._bursts.get_nowait())
