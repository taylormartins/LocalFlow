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
  * Voice commands (commands.py): one said on its own straight after a
    dictation ("submit", "scratch that"), or command mode, entered with the
    start phrase followed by "command mode" or by a command itself, in
    which everything said is taken as a command until "exit", the stop
    phrase, or command_mode_seconds of quiet.
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
from .commands import Ctx, Grammar, Parsed, Runner, describe, is_noise, plan_with_llm, resolvable, trim
from .config import Config
from .engine import State
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
#: A phrase scoring at least this (but under the accept level) is logged as
#: a near miss: "start phrase? heard 'okay listen merry gold' (0.78)".
NEAR = 0.7
#: In command mode, a request that isn't a command yet is held until this
#: much quiet after it: people pause mid-request ("bring my Chrome... Twitch window...
#: to the middle"), and each piece used to be taken as a command of its own.
HOLD = 1.8


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
        self._pending: Parsed | None = None  # heard before the paste finished
        self._cmd_lock = threading.Lock()
        self.grammar = Grammar(extra=cfg.always_on.commands)
        self.runner = Runner(self._command_done)
        # Command mode.
        self.commanding = False
        self.commanding_announced = False
        self._mode_until = 0  # absolute samples: leaves command mode after this much quiet
        self._mode_msg = ""
        #: Waiting for a confirmation: (plan, accepted answers, deadline in
        #: samples). An LLM plan that closes or sends waits for "yes"; a
        #: command with its own word (sleep: "goodnight") waits for that.
        self._confirm: tuple[list[Parsed], list[list[str]], int] | None = None
        self._dict_heard = False  # anything said yet in this dictation
        self._scratched_at = 0.0  # the dictation "scratch that" already removed
        #: The last phrase that came close but wasn't accepted: (which, heard).
        self.last_near: tuple[str, str] | None = None
        self._held: list[str] = []  # command mode: words of an unfinished request
        self._held_at = 0  # absolute samples: end of the held request's last burst
        self._llm_lock = threading.Lock()  # one LLM request at a time
        self._file_run = False  # run_file(): everything on the calling thread
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
        if not getattr(self, "_hooked", False):
            self.engine.on_state(self._on_engine_state)
            self._hooked = True
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
        self.leave_commands("Always-on off")
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
        if self.commanding and self.ring.end > self._mode_until:
            self.leave_commands("Command mode off")
        elif (self._held and not self._in_speech and not self._busy and self._bursts.empty()
              and self.ring.end - self._held_at > HOLD * RATE):
            request, self._held = " ".join(self._held), []
            if self._file_run:
                self._finish_request(request)  # repeatable timing in tests
            else:
                threading.Thread(target=self._finish_request, args=(request,), daemon=True,
                                 name="localflow-command-request").start()
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
                # inside it ("Okay listen, ...") would otherwise spend the
                # detector on half a phrase right as the rest is said. A
                # one-word command in its window gets checked early too.
                and t - self._burst_start >= (
                    int((0.4 + PREROLL) * RATE) if self._cmd_until > t or self.commanding
                    else self._peek_min()
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
        if self.commanding:
            self._command_burst(a, b, words, clip, fresh)
            return
        if not self.dictating and self._cmd_until > a:
            if self._check_command(a, words, fresh):
                return
        if not self.dictating:
            if self.engine.recording or self.engine.state.value not in ("idle", "error", "command"):
                return  # a hotkey dictation or processing is under way
            hit = self.detector.check("start", words, clip, after=fresh)
            if hit is None:
                self._near("start", words, fresh)
                return
            # "<start phrase>, switch to Chrome" / "..., command mode".
            rest = [w for w in words if w.start >= hit.end - 0.05]
            if rest and self._command_instead(a, rest):
                return
            self._begin(a, b, hit)
            # Start and stop in one breath: check the rest of this burst too.
            stop = self.detector.check("stop", words, clip, after=hit.end)
            if stop is not None:
                self._end(a, stop)
                self._check_command(a, words, (self._consumed - a) / RATE)
            return

        after = max(fresh, (self._dict_start - a) / RATE)
        if not self._dict_heard:
            # The first thing said after the start phrase, after a pause: a
            # command on its own ("...Marigold. Switch to Chrome.") means
            # command mode, not a dictation.
            new = [w for w in words if w.start >= after - 0.05]
            if new:
                self._dict_heard = True
                if (a + int(new[0].start * RATE) - self._dict_start < 4 * RATE
                        and self._command_instead(a, new)):
                    return
        hit = self.detector.check("stop", words, clip, after=after)
        if hit is not None:
            self._end(a, hit)
            # "...that will be all, submit" in one breath.
            self._check_command(a, words, (self._consumed - a) / RATE)
        else:
            self._near("stop", words, after)

    def _near(self, which: str, words, after: float) -> None:
        """Log a phrase that came close but wasn't accepted, so a missed
        phrase explains itself (and its spelling can be added from the tray).
        Only the few words that resembled the phrase are logged."""
        pool = [w for w in words if w.start >= after - 0.05]
        if not pool:
            return
        m = find([w.text for w in pool], self.detector.variants(which))
        if m is None or not NEAR <= m.score < self.detector.accept:
            return
        heard = " ".join(w.text for w in pool[m.first : m.last + 1])
        log.info("always-on: %s phrase? heard %r (%.2f, needs %.2f) - not accepted",
                 which, heard, m.score, self.detector.accept)
        self.last_near = (which, heard)
        self.on_event("near", f"{which}: {heard}")

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
            self._dict_heard = False
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

        "On its own" (the whole burst is the command) is the guard: "...that
        will be all. Submit the form tomorrow, then" is talk, not a command.
        Returns True if one was taken.
        """
        lo = max(fresh, (self._cmd_from - a) / RATE)
        hi = (self._cmd_until - a) / RATE
        pool = [w for w in words if w.start >= lo - 0.05]
        if not pool or pool[0].start > hi:
            return False
        texts = [w.text for w in pool]
        enter = self.grammar.is_one(texts, self.grammar.enter)
        p = None if enter else self.grammar.parse(texts)
        # A name that doesn't exist ("go to the shops") is talk, not a command.
        if not enter and (p is None or (p.arg and p.command.name != "click"
                                        and not resolvable(p))):
            if len(trim(texts)) <= 6:
                log.info("always-on: no command in %r", " ".join(texts))
                self.on_event("unknown", " ".join(texts))
            return False
        self._consumed = max(self._consumed, a + int(pool[-1].end * RATE))
        with self._cmd_lock:
            self._cmd_until = 0  # one command per dictation
            ready = self._delivered
            if not ready and p is not None:
                self._pending = p
        if enter:
            log.info("always-on: command mode (after a dictation)")
            self._enter_commands()
            return True
        log.info("always-on: command %r (%r)", p.command.name, " ".join(texts))
        self.on_event("command", p.command.name)
        if ready:
            self._do(p, after_dictation=True)
        return True

    def _delivered_now(self, text: str) -> None:
        """The dictation's text is in. Run a command heard while it was being
        transcribed, and keep the window open for one said in the next few
        seconds: on a long dictation the text appears only as the window
        would have closed, which is just when people say "submit"."""
        with self._cmd_lock:
            self._delivered = bool(text)
            p, self._pending = self._pending, None
            if text and self._cmd_until:
                self._cmd_until = max(
                    self._cmd_until, self.ring.end + int(self.cfg.always_on.command_window * RATE))
        if not (p and text):
            return
        if p.command.keys and p.command.name != "click":
            # On the pipeline thread, inside the target window if pasting
            # there: press it now, before LocalFlow switches back.
            if self.inject:
                try:
                    p.command.action(p.arg, self._ctx())
                except Exception as exc:
                    log.warning("always-on: %s not run: %s", p.command.name, exc)
            self.on_event("ran", str(p))
        else:
            self._do(p, after_dictation=True)

    # -- command mode -----------------------------------------------------------

    def _command_instead(self, a: int, words) -> bool:
        """Right after the start phrase: is this a command rather than the
        start of a dictation? Only if it's a command in full whose target
        exists, so "open the file and check it" is still dictated."""
        texts = [w.text for w in words]
        enter = self.grammar.is_one(texts, self.grammar.enter)
        p = None if enter else self.grammar.parse(texts)
        if not enter and (p is None or not resolvable(p)):
            return False
        if self.dictating:
            with self._lock:
                self.dictating = False
            self.engine.cancel_external()
        self._consumed = max(self._consumed, a + int(words[-1].end * RATE))
        self._enter_commands()
        if p is not None:
            log.info("always-on: command %r", p.command.name)
            self.on_event("command", p.command.name)
            self._do(p)
        return True

    def enter_commands(self) -> None:
        """Command mode from the tray (or anywhere outside the listener)."""
        if self.running and not self.dictating:
            self._enter_commands()

    def _enter_commands(self) -> None:
        self.commanding = True
        self._confirm = None
        self._extend_mode()
        if not self.commanding_announced:
            log.info("always-on: command mode")
            self.commanding_announced = True
        self.on_event("command-mode", "on")
        self._show("Command mode - say a command")

    def _extend_mode(self) -> None:
        self._mode_until = self.ring.end + int(self.cfg.always_on.command_mode_seconds * RATE)

    def leave_commands(self, message: str = "Command mode off") -> None:
        if not self.commanding:
            return
        self.commanding = False
        self.commanding_announced = False
        self._confirm = None
        self._held = []
        from .commands import hide_labels

        hide_labels()
        log.info("always-on: command mode off (%s)", message)
        self.on_event("command-mode", "off")
        if self.engine.state == State.COMMAND:
            self.engine.announce(State.IDLE, message)

    def _show(self, message: str) -> None:
        """Command mode's line in the pill (not over a dictation or a paste)."""
        self._mode_msg = message
        if self.commanding and not self.engine.recording and self.engine.state in (
                State.IDLE, State.ERROR, State.COMMAND):
            self.engine.announce(State.COMMAND, message)

    def _on_engine_state(self, state, detail: str) -> None:
        # A dictation or paste took over the pill; put command mode back after.
        if state == State.IDLE and self.commanding:
            threading.Timer(0.6, lambda: self._show(self._mode_msg)).start()

    def _command_burst(self, a: int, b: int, words, clip, fresh: float) -> None:
        if self.engine.recording or self.engine.state not in (
                State.COMMAND, State.IDLE, State.ERROR):
            return  # a hotkey dictation, or a paste, is under way
        pool = [w for w in words if w.start >= fresh - 0.05]
        texts = trim([w.text for w in pool])
        if is_noise(texts):
            return
        self._consumed = max(self._consumed, a + int(pool[-1].end * RATE))
        self._extend_mode()

        hit = self.detector.check("start", pool, clip)
        if hit is not None:
            # The start phrase: a command right after it stays in command
            # mode, anything else is a dictation.
            rest = [w for w in pool if w.start >= hit.end - 0.05]
            if rest and self._command_instead(a, rest):
                return
            self.leave_commands("Dictating")
            self._begin(a, b, hit)
            return
        if self.detector.check("stop", pool, clip) is not None or self.grammar.is_one(
                texts, self.grammar.exit):
            self.leave_commands()
            return
        if self._confirm is not None:
            plan, answers, deadline = self._confirm
            self._confirm = None
            if a < deadline and self.grammar.is_one(texts, answers):
                log.info("always-on: confirmed %s", ", ".join(map(str, plan)))
                for p in plan:
                    self._do(p, confirmed=True)
                return
            if self.grammar.is_one(texts, self.grammar.no):
                self._show("Cancelled")
                return
            self._show("Cancelled")
            if a < deadline:
                return  # an answer that wasn't the word: cancelled, nothing else
            # Too late: just a new request.
        if self.grammar.is_one(texts, self.grammar.enter):
            self._show("Command mode - say a command")
            return
        # The rest of a request begun in the last burst, or a command alone.
        if self._held:
            p = self.grammar.parse(self._held_words() + texts)
            if p is not None and self._usable(p):
                self._held = []
                self._take(p)
                return
        p = self.grammar.parse(texts)
        if p is not None and self._usable(p):
            self._held = []
            self._take(p)
            return
        # Not a command (yet): wait for the end of the sentence.
        self._held.append(" ".join(texts))
        self._held_at = b  # the burst's end: Whisper's word times run early
        self._show("…" + " ".join(self._held)[-30:])

    def _held_words(self) -> list[str]:
        return " ".join(self._held).split()

    def _take(self, p: Parsed) -> None:
        log.info("always-on: command %r", p.command.name)
        self.on_event("command", p.command.name)
        self._do(p)

    def _finish_request(self, request: str) -> None:
        """A pause ended an unrecognised request: one more try with the rules
        on the whole of it, then the LLM."""
        if not self.commanding:
            return
        p = self.grammar.parse(request.split())
        if p is not None and (self._usable(p) or not self._llm_on()):
            self._take(p)
        else:
            self._ask_llm(request)

    def _llm_on(self) -> bool:
        return self.cfg.always_on.command_llm and self.engine.cleaner.uses_llm

    def _usable(self, p: Parsed) -> bool:
        """A command naming a window or app that doesn't exist ("show me what's
        clickable" read as "switch to 'what'") goes to the LLM, if there is
        one, rather than failing."""
        return not p.arg or not self._llm_on() or resolvable(p)

    def _ask_llm(self, request: str) -> None:
        """Not a known command: let the LLM, if there is one, map it onto known
        commands. Without one, say what was heard so it can be rephrased."""
        cleaner = self.engine.cleaner
        if not self._llm_on():
            self._not_understood(request)
            return
        self._show("Working it out…")

        def go() -> None:
            with self._llm_lock:  # Ollama answers one at a time anyway
                try:
                    plan = plan_with_llm(cleaner._client, self.cfg.cleanup.model, request,
                                         keep_alive=self.cfg.cleanup.keep_alive)
                except Exception as exc:
                    log.warning("always-on: LLM for commands unavailable: %s", exc)
                    plan = []
            if not self.commanding:
                return
            if not plan:
                self._not_understood(request)
                return
            log.info("always-on: LLM plan %s", ", ".join(map(str, plan)))
            self.on_event("plan", ", ".join(map(str, plan)))
            if any(p.command.risky for p in plan):
                # Closing or sending something waits for a "yes".
                self._confirm = (plan, self.grammar.yes, self.ring.end + 10 * RATE)
                self._show("Say yes to: " + "; ".join(describe(p) for p in plan))
                return
            for p in plan:
                self._do(p)

        threading.Thread(target=go, daemon=True, name="localflow-command-llm").start()

    def _not_understood(self, request: str) -> None:
        log.info("always-on: not a command: %r", request[:80])
        self.on_event("unknown", request)
        self._show(f"Didn't catch: {request}")

    def _ctx(self) -> Ctx:
        h = self.engine.history[-1] if self.engine.history else None
        fresh = h is not None and h.at != self._scratched_at
        return Ctx(
            last_text=h.text if fresh else "", last_at=h.at if h else 0.0,
            last_target=self.engine.last_target if self.engine.targeted else None,
            paste=self.engine.injector.send, in_target=self.engine.in_target,
            last_any=h.text if h else "",
        )

    def _do(self, p: Parsed, after_dictation: bool = False, confirmed: bool = False) -> None:
        """Run a command on the command thread. After a dictation, it waits
        for the paste to finish, and a key command goes to the window the text
        went into. One with a confirmation word (sleep) only arms itself."""
        if p.command.confirm and not confirmed:
            self._arm(p)
            return
        self.on_event("ran", str(p))
        if p.command.name == "scratch" and self.engine.history:
            self._scratched_at = self.engine.history[-1].at
        if not self.inject:
            if self.commanding:
                self._show(f"(test) {describe(p)}")
            return
        wrap = None
        if after_dictation:
            target = self.engine.last_target if self.engine.targeted else None

            def wrap(go: Callable[[], None]) -> None:
                deadline = time.monotonic() + 5.0
                while self.engine.state == State.INJECTING and time.monotonic() < deadline:
                    time.sleep(0.05)
                if p.command.keys:
                    self.engine.in_target(target, go)
                else:
                    go()

        self.runner.submit(p, self._ctx(), wrap)

    def _arm(self, p: Parsed) -> None:
        """Triple click, then wait for the command's own word ("goodnight")."""
        word = p.command.confirm[0]
        if not self.commanding:
            self._enter_commands()
        data = None
        try:
            from .sounds import _duration, play, render

            data = render("triple click", self.cfg.sounds.volume or 0.5)
            play(data)
        except Exception:
            log.debug("triple click failed", exc_info=True)
        # The clicks themselves (picked up by the mic) are not an answer.
        cue = (_duration(data) if data else 0.3) + 0.3
        self._consumed = max(self._consumed, self.ring.end + int(cue * RATE))
        self._confirm = ([p], [normalise(w) for w in p.command.confirm],
                         self.ring.end + int((cue + 10) * RATE))
        log.info("always-on: %s armed - waiting for %r", p.command.name, word)
        self.on_event("confirm", p.command.name)
        self._show(f'Say "{word}" to {describe(p)}')

    def _command_done(self, p: Parsed, message: str, ok: bool) -> None:
        self.on_event("done" if ok else "failed", message)
        if self.commanding:
            self._show(message if ok else f"✗ {message}")
        elif not ok and self.engine.state == State.IDLE:
            self.engine.announce(State.ERROR, message)

    # -- testing -------------------------------------------------------------

    def feed_realtime(self, block: np.ndarray) -> None:
        """Hand 16 kHz audio to the running listener as the mic would."""
        self._blocks.put(np.asarray(block, dtype=np.float32))

    def run_file(self, audio: np.ndarray, chunk: float = 0.1) -> None:
        """Push a 16 kHz recording through as if it came from the mic, in real
        order but without real time, waiting for detection after each chunk so
        timing-dependent behaviour matches live use."""
        self.running = True
        self._file_run = True
        self.engine.external_stop = self.finish
        step = int(chunk * RATE)
        for i in range(0, len(audio), step):
            self.ingest(audio[i : i + step])
            while not self._bursts.empty():
                self._check(*self._bursts.get_nowait())
                self._wait_for_pipeline()
        # Trailing quiet so a final burst ends.
        self.ingest(np.zeros(int(1.0 * RATE), dtype=np.float32))
        while not self._bursts.empty():
            self._check(*self._bursts.get_nowait())
            self._wait_for_pipeline()

    def _wait_for_pipeline(self) -> None:
        """In a file run, a finished dictation is processed in no audio time,
        as if the transcription were instant, so timings stay repeatable."""
        busy = (State.RECORDING, State.TRANSCRIBING, State.CLEANING, State.STYLING,
                State.INJECTING)
        deadline = time.monotonic() + 60
        while (not self.dictating and not self.engine.recording
               and self.engine.state in busy and time.monotonic() < deadline):
            time.sleep(0.02)
