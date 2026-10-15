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
from collections import deque
from collections.abc import Callable

import numpy as np

from .audio import resolve_candidates
from .config import Config
from .wake import RATE, Detector, Hit, Phrases, find, normalise, strip_phrases

log = logging.getLogger(__name__)

FRAME = 480  # 30 ms
RING_SECONDS = 30
PREROLL = 0.3  # kept before the first loud frame: soft consonants start quiet
HANGOVER = 0.5  # quiet that ends a burst
#: Quiet after which the burst so far is checked early, if the detector is
#: free: most people pause briefly after a phrase, and waiting out the whole
#: HANGOVER was a third of the delay before the start cue.
PEEK = 0.2
MAX_BURST = 6.0
OVERLAP = 2.0
#: Margins at the cuts. Whisper's word times are good to ~0.1 s; erring into
#: the phrase is fixed by strip_phrases, erring into the dictation is not.
START_MARGIN = 0.08
STOP_MARGIN = 0.05


def _press_enter() -> None:
    # A moment for the target app to take in the paste first: Ctrl+V is
    # processed asynchronously, and Enter overtaking it would send nothing.
    time.sleep(0.15)
    from .hotkey import _tap

    _tap(0x0D)  # VK_RETURN, marked as our own input so the hook ignores it


#: Actions for the voice commands in [always_on].commands.
COMMANDS: dict[str, Callable[[], None]] = {
    "submit": _press_enter,
}


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
        #: Absolute sample before which words were already used by a phrase,
        #: so an early check and the full burst can never trigger twice.
        self._consumed = 0
        self._busy = False
        self._peeked: tuple[int, int] | None = None  # (burst start, last loud) at the peek
        # Voice commands after the stop phrase (see _command).
        self._cmd_from = 0  # absolute samples: the window opens at the stop phrase's end...
        self._cmd_until = 0  # ...and closes command_window seconds later
        self._delivered = False  # this dictation's text is in (paste done)
        self._pending: str | None = None  # heard before the paste finished
        self._cmd_lock = threading.Lock()
        self._commands = {
            name: [normalise(s) for s in spellings if normalise(s)]
            for name, spellings in cfg.always_on.commands.items()
        }
        self._levels: deque[float] = deque(maxlen=int(2.0 * RATE / FRAME))
        self._since_floor = 0
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

    def start(self, stream: bool = True) -> None:
        """Start listening. ``stream=False`` (tests) runs everything but the
        mic: audio then arrives through feed_realtime()."""
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
        if stream:
            self._open_stream()
        else:
            self._rate = RATE
        self.engine.external_stop = self.finish
        # On the GPU when the dictation model is: ~10x faster than the CPU,
        # and the phrase detection time is most of the wait for the cue.
        self.detector.device = "cuda" if self.engine.transcriber.device_in_use == "cuda" else "cpu"
        # Load the listening model now, not on the first phrase.
        threading.Thread(target=self.detector.warm, daemon=True, name="localflow-wake-load").start()
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
            # Noise floor: a low percentile of the last 2 s, so it follows
            # the room both ways within a couple of seconds. Exact digital
            # silence is left out: mics with noise suppression or a gate
            # output zeros between words, and an earlier creeping floor sank
            # to nothing on those. Room hiss then counted as speech for about
            # two minutes, so no burst ended and the stop phrase went unheard.
            if r > 1e-5:
                self._levels.append(r)
                self._since_floor += 1
                if self._since_floor >= 5 and len(self._levels) >= 10:
                    self._floor = float(np.percentile(self._levels, 15))
                    self._since_floor = 0
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
            quiet_s = self._quiet * FRAME / RATE
            if quiet_s >= HANGOVER:
                if self._peeked != (self._burst_start, self._last_speech):
                    self._emit(self._burst_start, t - self._quiet * FRAME + int(0.2 * RATE))
                # else: nothing was said since the early check of this burst.
                self._in_speech, self._run = False, 0
            elif (
                self._quiet == int(PEEK * RATE / FRAME)
                and not self._busy and self._bursts.empty()
                # Only once the burst could hold the whole phrase: a pause
                # inside it ("Listen to me, ...") would otherwise spend the
                # detector on half a phrase right as the rest is said. A
                # one-word command in its window gets checked early too.
                and t - self._burst_start >= (
                    int((0.4 + PREROLL) * RATE) if self._cmd_until > t else self._peek_min()
                )
            ):
                self._peeked = (self._burst_start, self._last_speech)
                self._emit(self._burst_start, t)
            elif t - self._burst_start >= MAX_BURST * RATE:
                self._emit(self._burst_start, t)
                self._burst_start = t - int(OVERLAP * RATE)

    def _peek_min(self) -> int:
        """Samples a burst needs before an early check: 80 % of the shortest
        setup take of the phrase we're waiting for, plus the pre-roll."""
        which = "stop" if self.dictating else "start"
        frames = self.detector.shortest_take(which)  # 20 ms feature frames
        seconds = 0.8 * frames * 0.02 if frames else 0.8
        return int((seconds + PREROLL) * RATE)

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
            self._busy = True
            try:
                self._check(*item)
            except Exception:
                log.exception("always-on: detection failed")
            finally:
                self._busy = False

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

        fresh = max(0.0, (self._consumed - a) / RATE)  # skip words a phrase already used
        if not self.dictating and self._cmd_until > a:
            if self._check_command(a, words, fresh):
                return
        if not self.dictating:
            if self.engine.recording or self.engine.state.value not in ("idle", "error"):
                return  # a hotkey dictation or processing is under way
            hit = self.detector.check("start", words, clip, after=fresh)
            if hit is None:
                return
            self._begin(a, b, hit)
            # Start and stop in one breath: check the rest of this burst too.
            stop = self.detector.check("stop", words, clip, after=hit.end)
            if stop is not None:
                self._end(a, stop)
                self._check_command(a, words, (self._consumed - a) / RATE)
            return

        after = max(fresh, (self._dict_start - a) / RATE)
        hit = self.detector.check("stop", words, clip, after=after)
        if hit is not None:
            self._end(a, hit)
            # "...that will be all, submit" in one breath.
            self._check_command(a, words, (self._consumed - a) / RATE)

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
            self._consumed = max(self._consumed, a + int(hit.end * RATE))
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
        end = a + int(hit.end * RATE)
        self._consumed = max(self._consumed, end)
        with self._cmd_lock:
            self._cmd_from = end
            self._cmd_until = end + int(self.cfg.always_on.command_window * RATE)
            self._delivered, self._pending = False, None
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
            keep, transform=lambda raw: strip_phrases(raw, start, stop), inject=self.inject,
            done=self._delivered_now,
        )

    # -- voice commands after the stop phrase -------------------------------

    def _check_command(self, a: int, words, fresh: float) -> bool:
        """A command said on its own, inside the window after the stop phrase.

        "On its own" (the whole burst is the command, give or take a word) is
        the guard: "...that will be all. Submit the form tomorrow, then" is
        talk, not a command. Returns True if one was taken.
        """
        lo = max(fresh, (self._cmd_from - a) / RATE)
        hi = (self._cmd_until - a) / RATE
        pool = [w for w in words if w.start >= lo - 0.05]
        if not pool or pool[0].start > hi:
            return False
        texts = [w.text for w in pool]
        for name, spellings in self._commands.items():
            m = find(texts, spellings)
            if m is None or m.score < self.detector.accept:
                continue
            longest = max(len(s) for s in spellings)
            if len(texts) > longest + 1:
                continue  # part of a sentence
            self._consumed = max(self._consumed, a + int(pool[m.last].end * RATE))
            with self._cmd_lock:
                self._cmd_until = 0  # one command per dictation
                ready = self._delivered
                if not ready:
                    self._pending = name
            log.info("always-on: command %r (%r)", name, " ".join(texts))
            self.on_event("command", name)
            if ready:
                self._run_command(name)
            return True
        return False

    def _delivered_now(self, text: str) -> None:
        """The dictation's text is in. Run a command heard while it was being
        transcribed, or let one said in the next few seconds run at once."""
        with self._cmd_lock:
            self._delivered = bool(text)
            name, self._pending = self._pending, None
        if name and text:
            # On the pipeline thread, inside the target window if pasting
            # there: run it now, before LocalFlow switches back.
            self._run_command(name, now=True)

    def _run_command(self, name: str, now: bool = False) -> None:
        action = COMMANDS.get(name)
        if action is None:
            log.warning("always-on: no action for command %r", name)
            return
        if self.inject:
            if now:
                action()
            else:
                # Said after the paste: go back to that window to do it.
                target = self.engine.last_target if self.engine.targeted else None

                def run() -> None:
                    try:
                        self.engine.in_target(target, action)
                    except Exception as exc:
                        log.warning("always-on: %s not run: %s", name, exc)

                threading.Thread(target=run, daemon=True, name=f"localflow-cmd-{name}").start()
        self.on_event("ran", name)

    # -- testing -------------------------------------------------------------

    def feed_realtime(self, block: np.ndarray) -> None:
        """Hand 16 kHz audio to the running listener as the mic would."""
        self._blocks.put(np.asarray(block, dtype=np.float32))

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
