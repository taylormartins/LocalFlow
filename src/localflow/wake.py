"""Start/stop phrases for always-on mode: storage, matching, detection.

A phrase is recognised two ways, and both come from the setup takes:

  * Text. A small Whisper model transcribes each burst of speech, and the
    words are fuzzy-matched against every known spelling of the phrase: the
    one typed in, any alternatives typed in, and - most useful - whatever the
    model actually heard during setup ("okay listen, merry gold"), because that is
    what it will hear next time too.
  * Sound. Each setup take is kept as a voice template (log-mel features).
    A phrase that reads right but doesn't line up with the templates under
    dynamic time warping is vetoed. The voice can only refuse, never accept:
    as a tie-breaker for near-miss text it let "Mary told" pass for
    "marigold", which really does sound alike.

No initial prompt is given to the detection model: priming Whisper with the
phrase makes it "hear" the phrase in noise, which is the false trigger this
whole design is trying to avoid.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

WAKE_DIR = PROJECT_ROOT / "wake"
STORE = WAKE_DIR / "phrases.json"
RATE = 16000
TAKES = 3  # per phrase: enough to learn the spread, few enough not to annoy

_CONTRACTIONS = {
    "we're": "we are", "you're": "you are", "they're": "they are", "i'm": "i am",
    "it's": "it is", "that's": "that is", "let's": "let us", "we've": "we have",
    "i've": "i have", "don't": "do not", "can't": "cannot", "won't": "will not",
}


def normalise(text: str) -> list[str]:
    """Lowercase words, punctuation gone, contractions expanded."""
    text = text.lower().replace("’", "'")
    words = []
    for w in re.findall(r"[a-z0-9']+", text):
        words.extend(_CONTRACTIONS.get(w, w.strip("'")).split())
    return [w for w in words if w]


@dataclass
class Phrase:
    text: str = ""
    #: Every accepted spelling, as normalised word lists joined by spaces.
    variants: list[str] = field(default_factory=list)
    #: wav files (relative to WAKE_DIR) of the setup takes.
    takes: list[str] = field(default_factory=list)
    #: DTW distance under which a near-miss counts, learned from the takes.
    dtw_limit: float = 0.0
    #: What the listening model heard for each take (shown again in setup).
    heard: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        # Takes too: typed text alone is never enough to switch listening on.
        return bool(self.text and self.variants and len(self.takes) >= TAKES)

    def all_variants(self) -> list[list[str]]:
        seen, out = set(), []
        for v in [self.text, *self.variants]:
            words = normalise(v)
            key = " ".join(words)
            if words and key not in seen:
                seen.add(key)
                out.append(words)
        return out


@dataclass
class Phrases:
    start: Phrase = field(default_factory=Phrase)
    stop: Phrase = field(default_factory=Phrase)

    @property
    def ready(self) -> bool:
        return self.start.ready and self.stop.ready

    @classmethod
    def load(cls, path: Path | None = None) -> Phrases:
        path = path or WAKE_DIR / "phrases.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            known = set(Phrase.__dataclass_fields__)
            parts = {k: Phrase(**{f: v for f, v in data.get(k, {}).items() if f in known})
                     for k in ("start", "stop")}
            return cls(**parts)
        except FileNotFoundError:
            return cls()
        except Exception as exc:
            log.warning("could not read %s: %s", path, exc)
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or WAKE_DIR / "phrases.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: getattr(self, k).__dict__ for k in ("start", "stop")}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


# -- text matching -----------------------------------------------------------

@dataclass
class Match:
    score: float
    first: int  # index of the first matched word
    last: int  # index of the last matched word (inclusive)


#: Every word must be at least this close to its counterpart in a spelling.
WORD_FLOOR = 0.85
#: A phrase that reads right is still refused if it sounds further from the
#: setup takes than this many times their own spread, and further than
#: VOICE_VETO_FLOOR. Measured with one TTS voice: takes 0.02-0.04 apart, the
#: real phrase cut from running speech 0.05-0.13 (word times blur its edges),
#: other sentences 0.17-0.33. Without the floor, three near-identical takes
#: set a limit so tight that the genuine phrase in context was refused.
VOICE_VETO = 2.5
VOICE_VETO_FLOOR = 0.2


def find(words: list[str], variants: list[list[str]]) -> Match | None:
    """Best occurrence of any spelling inside ``words``.

    The score is the *worst* word's similarity under the best alignment, so
    one different word sinks the match however well the rest agree: with
    "marigold" in the phrase, "okay listen, Mary told me" scores
    sim("marytold", "marigold") = 0.75, not the 0.9 its letters as a whole
    would. Similar-sounding words are exactly where false triggers come from;
    real mishearings are covered by the spellings learned at setup instead.
    The alignment lets Whisper split or merge a word: "mary gold" lines up
    with "marigold" as one unit.
    """
    best: Match | None = None
    for v in variants:
        for size in {len(v) - 1, len(v), len(v) + 1}:
            if size < 1 or size > len(words):
                continue
            for i in range(len(words) - size + 1):
                score = _aligned(words[i : i + size], v)
                if best is None or score > best.score:
                    best = Match(score, i, i + size - 1)
    return best


def _aligned(heard: list[str], want: list[str]) -> float:
    """Worst-word similarity of the best alignment, allowing 2:1 and 1:2 merges."""
    n, m = len(heard), len(want)
    f = [[-1.0] * (m + 1) for _ in range(n + 1)]
    f[0][0] = 1.0
    for i in range(n + 1):
        for j in range(m + 1):
            if f[i][j] < 0:
                continue
            for di, dj in ((1, 1), (2, 1), (1, 2)):
                if i + di <= n and j + dj <= m:
                    s = _merged(heard[i : i + di], want[j : j + dj])
                    f[i + di][j + dj] = max(f[i + di][j + dj], min(f[i][j], s))
    return max(f[n][m], 0.0)


#: A word split or merged by Whisper ("mary gold" for "marigold") only counts
#: if the letters are the same but for the space.
MERGE_FLOOR = 0.95


def _merged(a: list[str], b: list[str]) -> float:
    """Similarity of 1-2 words against 1-2 words.

    A split or merge must be essentially exact. Letters can't tell "mary
    told" from "mary gold": one letter apart, 0.875 as a string and 0.857 at
    the cleverest split ("maryg|old" vs "mary|told"). So a split only counts
    when nothing but the space changed. Real split spellings ("mary gold")
    are learned from the setup takes; a one-letter slip inside a single word
    ("merigold") still gets the ordinary allowance.
    """
    if len(a) == len(b):
        return min(_sim(x, y) for x, y in zip(a, b))
    s = _sim("".join(a), "".join(b))
    return s if s >= MERGE_FLOOR else min(s, 0.5)


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def strip_phrases(text: str, start: list[list[str]], stop: list[list[str]], at: float = 0.8) -> str:
    """Remove what the audio cuts left of the phrases: the tail of the start
    phrase at the beginning ("Marigold. Send it...") and the head of the stop
    phrase at the end ("...to Sam, we are."), or either phrase whole.

    A leftover must look like the matching end of the phrase. A single word
    only counts if it's long and close (a name like "marigold"), so a short
    real word ("we", "me") that merely resembles it is never eaten.
    """
    tokens = text.split()
    if not tokens:
        return text

    def norm(i: int, j: int) -> list[str]:
        return normalise(" ".join(tokens[i:j]))

    for variants, at_start in ((start, True), (stop, False)):
        best_n, best_score = 0, at
        for v in variants:
            for n in range(1, min(len(tokens), len(v) + 1) + 1):
                got = norm(0, n) if at_start else norm(len(tokens) - n, len(tokens))
                if not got:
                    continue
                # Start: compare with the phrase's last words. Stop: its first.
                k = min(len(got), len(v))
                part = v[-k:] if at_start else v[:k]
                score = _sim(" ".join(got), " ".join(part))
                if len(got) == 1 and (len(got[0]) < 4 or score < 0.85):
                    continue
                if score >= best_score:
                    best_n, best_score = n, score
        if best_n:
            if at_start:
                tokens = tokens[best_n:]
                if tokens:
                    tokens[0] = tokens[0].lstrip(",.;:-")
                    tokens[0] = tokens[0][:1].upper() + tokens[0][1:]
            else:
                tokens = tokens[: len(tokens) - best_n]
                if tokens:
                    # "...to Sam, that will be all." -> "...to Sam."
                    tokens[-1] = tokens[-1].rstrip(",;:-")
                    if tokens[-1][-1:].isalnum():
                        tokens[-1] += "."
    return " ".join(t for t in tokens if t)


# -- sound matching ----------------------------------------------------------

_extractor = None


def features(audio: np.ndarray) -> np.ndarray:
    """Log-mel frames (time, 80), mean/variance normalised, 20 ms apart."""
    global _extractor
    if _extractor is None:
        from faster_whisper.feature_extractor import FeatureExtractor

        _extractor = FeatureExtractor()
    mel = _extractor(audio.astype(np.float32), padding=0)  # (80, frames)
    mel = mel[:, ::2].T.astype(np.float32)
    mel -= mel.mean(axis=0)
    mel /= mel.std(axis=0) + 1e-5
    return mel


def dtw(a: np.ndarray, b: np.ndarray) -> float:
    """Average cosine distance along the best alignment of two feature runs."""
    if len(a) == 0 or len(b) == 0:
        return float("inf")
    an = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-8)
    bn = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-8)
    cost = 1.0 - an @ bn.T
    n, m = cost.shape
    acc = np.full((n + 1, m + 1), np.inf)
    acc[0, 0] = 0.0
    # Row by row, vectorised along the row except for the left neighbour.
    for i in range(1, n + 1):
        diag_up = np.minimum(acc[i - 1, :-1], acc[i - 1, 1:]) + cost[i - 1]
        row = acc[i]
        for j in range(1, m + 1):
            row[j] = min(diag_up[j - 1], row[j - 1] + cost[i - 1, j - 1])
    return float(acc[n, m] / (n + m))


def trim(audio: np.ndarray, pad: float = 0.08) -> np.ndarray:
    """Cut leading/trailing silence (relative to the take's own loudness)."""
    if audio.size < 480:
        return audio
    frames = audio[: audio.size // 480 * 480].reshape(-1, 480)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    loud = np.flatnonzero(rms > max(rms.max() * 0.08, 1e-4))
    if loud.size == 0:
        return audio[:0]
    p = int(pad * RATE)
    return audio[max(0, loud[0] * 480 - p) : min(audio.size, (loud[-1] + 1) * 480 + p)]


def learn_limit(templates: list[np.ndarray]) -> float:
    """DTW limit from how far apart the user's own takes are: a little looser
    than the worst pair, so a fourth take of the same phrase passes but a
    similar-sounding phrase does not."""
    dists = [dtw(a, b) for i, a in enumerate(templates) for b in templates[i + 1 :]]
    return max(dists) * 1.15 if dists else 0.0


def expand(alternate: str, phrase: str) -> list[str]:
    """An alternative spelling as a whole phrase.

    Typing just "Merrigold" must mean "okay listen merrigold", never the bare word:
    a one-word variant would fire every time the name came up in conversation.
    So a shorter alternative replaces the stretch of the phrase it most
    resembles; a full-length one is used as is.
    """
    alt, words = normalise(alternate), normalise(phrase)
    if not alt or len(alt) >= len(words):
        return alt
    best, at = -1.0, 0
    for i in range(len(words) - len(alt) + 1):
        score = _sim(" ".join(alt), " ".join(words[i : i + len(alt)]))
        if score > best:
            best, at = score, i
    if best < 0.5:
        return []  # resembles nothing in the phrase: no safe place to put it
    return words[:at] + alt + words[at + len(alt) :]


def make_phrase(
    which: str, text: str, alternates: list[str], takes: list[np.ndarray], heard: list[str]
) -> Phrase:
    """Save a phrase's takes as voice templates and gather its spellings: the
    phrase as typed, typed alternatives, and what the model heard each time."""
    names = []
    for i, audio in enumerate(takes):
        name = f"{which}_{i + 1}.wav"
        write_wav(WAKE_DIR / name, audio)
        names.append(name)
    good_heard = [h for h in heard if h and not h.startswith("(")]
    candidates = [expand(a, text) for a in alternates] + [normalise(h) for h in good_heard]
    variants = list(dict.fromkeys(" ".join(v) for v in candidates if v))
    return Phrase(
        text=text.strip(), variants=variants, takes=names,
        dtw_limit=learn_limit([features(a) for a in takes]), heard=list(heard),
    )


# -- detection ---------------------------------------------------------------

@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Hit:
    which: str  # "start" or "stop"
    score: float
    start: float  # seconds into the clip where the phrase begins
    end: float  # ... and ends
    heard: str
    by: str  # "text" or "text+voice"
    #: Start of the word after the phrase / end of the word before it, if any.
    #: Whisper's end time for the last word of a burst often runs short, so
    #: the cuts lean on the neighbouring words where there are some.
    next_start: float | None = None
    prev_end: float | None = None


class Detector:
    """The small listening model plus the phrases it listens for."""

    def __init__(self, model: str, phrases: Phrases, accept: float = WORD_FLOOR):
        self.model_name = model
        self.accept = accept
        self.device = "cpu"  # the listener sets "cuda" when the GPU is in use
        self._model = None
        self._lock = threading.Lock()
        self.set_phrases(phrases)

    def set_phrases(self, phrases: Phrases) -> None:
        self.phrases = phrases
        self._variants = {k: getattr(phrases, k).all_variants() for k in ("start", "stop")}
        self._templates: dict[str, list[np.ndarray]] = {}
        for k in ("start", "stop"):
            feats = []
            for name in getattr(phrases, k).takes:
                try:
                    feats.append(features(read_wav(WAKE_DIR / name)))
                except Exception as exc:
                    log.warning("voice template %s unusable: %s", name, exc)
            self._templates[k] = feats

    def warm(self) -> None:
        """Load the model and run it once on a real setup take. The first
        inference pays one-off costs (CUDA kernels, the VAD model, the
        word-alignment path): ~1 s on the GPU, which otherwise landed on the
        first start phrase after every boot."""
        self.load()
        takes = self.phrases.start.takes
        if takes:
            try:
                self.words(read_wav(WAKE_DIR / takes[0]))
            except Exception as exc:
                log.debug("always-on: warm-up skipped: %s", exc)

    def shortest_take(self, which: str) -> int:
        """Length of the shortest setup take, in feature frames (20 ms)."""
        t = self._templates.get(which) or []
        return min(len(x) for x in t) if t else 0

    def variants(self, which: str) -> list[list[str]]:
        return self._variants[which]

    def load(self):
        with self._lock:
            if self._model is None:
                from .models import small_whisper

                self._model, self.device = small_whisper(self.model_name, self.device)
        return self._model

    def words(self, audio: np.ndarray) -> list[Word]:
        """Transcribe a short clip into timed, normalised words."""
        model = self.load()
        # temperature 0, once: a phrase cut off mid-way (the early check at a
        # short pause) looks "uncertain", and Whisper's fallback then decoded
        # it six times at rising temperatures - 6.7 s instead of 1.3 s on a
        # 1.3 s clip. The token cap bounds a looping decode on noise.
        segments, _ = model.transcribe(
            audio, language="en", beam_size=1, word_timestamps=True,
            vad_filter=True, condition_on_previous_text=False,
            temperature=0.0, max_new_tokens=64,
        )
        out = []
        for seg in segments:
            for w in seg.words or []:
                for piece in normalise(w.word):
                    out.append(Word(piece, w.start, w.end))
        return out

    def check(self, which: str, words: list[Word], audio: np.ndarray, after: float = 0.0) -> Hit | None:
        """Is the ``which`` phrase in these words (at or after ``after`` s)?"""
        pool = [w for w in words if w.start >= after - 0.05]
        if not pool:
            return None
        m = find([w.text for w in pool], self._variants[which])
        if m is None or m.score < self.accept:
            return None
        first, last = pool[m.first], pool[m.last]
        heard = " ".join(w.text for w in pool[m.first : m.last + 1])
        by = "text"
        # The voice can only veto. It was tried as a rescue for near-miss text
        # and let "okay listen, Mary told me" through for "...marigold": the
        # two genuinely sound alike. As a veto it stops a phrase that reads
        # right but sounds nothing like your takes (a misheard burst of TV).
        limit = getattr(self.phrases, which).dtw_limit
        templates = self._templates.get(which) or []
        if templates and limit > 0:
            clip = audio[int(first.start * RATE) : int(last.end * RATE) + 1]
            feats = features(trim(clip)) if clip.size else clip
            dist = min(dtw(feats, t) for t in templates) if len(feats) else float("inf")
            veto = max(limit * VOICE_VETO, VOICE_VETO_FLOOR)
            log.debug("always-on: %s %r text %.2f, voice %.3f (veto above %.3f)",
                      which, heard, m.score, dist, veto)
            if dist > veto:
                log.info("always-on: %s phrase %r vetoed - doesn't sound like the setup takes",
                         which, heard)
                return None
            by = "text+voice"
        after_word = pool[m.last + 1].start if m.last + 1 < len(pool) else None
        before_word = pool[m.first - 1].end if m.first > 0 else None
        return Hit(which, m.score, first.start, last.end, heard, by, after_word, before_word)
