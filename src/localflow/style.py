"""Style pass: rewrite the cleaned dictation in a character voice and/or tone.

Runs after cleanup, as one extra Ollama call, only when a character or tone is
picked (tray: Character / Tone). The two stack: "rogue" + "rage" is one request.

Tone follows Plutchik's wheel of emotions - 8 emotions, each at three
intensities (annoyance -> anger -> rage). Every level carries hand-written
anchor words; if data/tone_vocab.json exists (built from the NRC Emotion
Intensity Lexicon by tools/build_tone_vocab.py, not redistributable so not in
git) its words for the emotion are offered too, at the intense level only -
below that the lexicon is association noise rather than tone vocabulary.

Like cleanup, a rewrite that looks like the model *answering* the dictation is
rejected and the cleaned text is used instead. You never lose words.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from .cleanup import _strip_wrappers
from .config import PROJECT_ROOT, CleanupConfig, StyleConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Level:
    name: str
    emotion: str
    intensity: str  # mild | moderate | intense
    feel: str
    anchors: tuple[str, ...]


def _levels(emotion: str, *rows: tuple[str, str, tuple[str, ...]]) -> list[Level]:
    return [
        Level(name, emotion, intensity, feel, anchors)
        for (name, feel, anchors), intensity in zip(rows, ("mild", "moderate", "intense"))
    ]


#: Plutchik's wheel, outer ring (mild) to inner ring (intense), in wheel order.
WHEEL: dict[str, list[Level]] = {
    "joy": _levels(
        "joy",
        ("serenity", "calm contentment, quietly pleased", ("content", "at ease", "pleased", "glad", "comfortable")),
        ("joy", "openly happy and warm", ("happy", "delighted", "cheerful", "love", "great")),
        ("ecstasy", "overjoyed and exuberant", ("overjoyed", "ecstatic", "elated", "thrilled", "over the moon")),
    ),
    "trust": _levels(
        "trust",
        ("acceptance", "agreeable, fine with it", ("fair enough", "fine by me", "reasonable", "makes sense", "okay with")),
        ("trust", "confident and reassuring", ("rely on", "confident", "count on", "believe in", "dependable")),
        ("admiration", "deeply respectful and impressed", ("admire", "remarkable", "inspiring", "outstanding", "look up to")),
    ),
    "fear": _levels(
        "fear",
        ("apprehension", "a little uneasy and hesitant", ("uneasy", "not sure", "hesitant", "a bit worried", "cautious")),
        ("fear", "worried and nervous", ("afraid", "worried", "nervous", "scared", "alarming")),
        ("terror", "panicked and terrified", ("terrified", "panicking", "petrified", "horrified", "dread")),
    ),
    "surprise": _levels(
        "surprise",
        ("distraction", "mildly sidetracked, a passing 'oh'", ("oh", "huh", "by the way", "wait", "come to think of it")),
        ("surprise", "surprised, didn't expect it", ("surprised", "unexpected", "didn't see that coming", "wow", "really")),
        ("amazement", "astonished, blown away", ("astonished", "stunned", "unbelievable", "mind-blowing", "jaw-dropping")),
    ),
    "sadness": _levels(
        "sadness",
        ("pensiveness", "wistful and reflective", ("wistful", "a little down", "I suppose", "reflective", "quietly")),
        ("sadness", "sad and disappointed", ("sad", "disappointed", "sorry", "unhappy", "heavy-hearted")),
        ("grief", "heartbroken and devastated", ("heartbroken", "devastated", "crushed", "gutted", "inconsolable")),
    ),
    "disgust": _levels(
        "disgust",
        ("boredom", "bored and unimpressed", ("tedious", "dull", "whatever", "tiresome", "meh")),
        ("disgust", "put off and distasteful", ("gross", "distasteful", "off-putting", "unpleasant", "nasty")),
        ("loathing", "utterly repulsed", ("repulsive", "despicable", "vile", "can't stand", "abhorrent")),
    ),
    "anger": _levels(
        "anger",
        ("annoyance", "mildly irritated", ("annoyed", "irritating", "bothered", "a bit frustrating", "come on")),
        ("anger", "angry and fed up", ("angry", "fed up", "unacceptable", "mad", "ridiculous")),
        ("rage", "furious, barely holding it together", ("furious", "livid", "seething", "absolutely unacceptable", "enraged")),
    ),
    "anticipation": _levels(
        "anticipation",
        ("interest", "curious and intrigued", ("curious", "intrigued", "interested", "wondering", "keen to know")),
        ("anticipation", "eager and looking forward", ("looking forward", "eager", "can't wait", "excited", "ready")),
        ("vigilance", "on high alert, watching closely", ("alert", "watching closely", "ready for anything", "on guard", "keeping an eye")),
    ),
}
LEVELS: dict[str, Level] = {lv.name: lv for levels in WHEEL.values() for lv in levels}

#: Built-in voices. Add your own under [style.characters] in config.toml.
CHARACTERS: dict[str, str] = {
    "humanize": "a natural, warm person talking to a friend: contractions, plain words, no corporate phrasing",
    "professional": "a polished business professional: clear, concise, courteous, no slang",
    "tech professional": "a senior software engineer: precise, direct, technical vocabulary where it fits",
    "doctor": "a calm, reassuring physician: measured, careful, precise without heavy jargon",
    "rogue": "a sly, swaggering rogue from a fantasy video game: wry, cheeky, a touch of tavern slang",
    "robot": "a literal-minded robot: clipped, formal, mechanical phrasing, no contractions",
    "caring mother": "a warm, caring mother: gentle, encouraging, nurturing",
    "defiant worker": "a fed-up, defiant worker: blunt, pushes back, refuses to sugar-coat",
}

#: Openers that mean the model replied instead of rewriting. Narrower than
#: cleanup's list on purpose: "Unfortunately," or "No way," can be a legitimate
#: opening once a tone is applied.
_ASSISTANT_OPENERS = (
    "as an ai", "great question", "good question", "id be happy", "happy to help",
    "heres a", "heres the", "heres your", "sure heres", "certainly", "of course i",
    "i cant", "i can help", "rewritten", "here is",
)

#: Worked examples carry the behaviour, as in cleanup.py: the system prompt
#: alone let qwen2.5:7b turn "Tell me a joke." into a 20x rant or an actual
#: joke. Each shows the tone through word choice, at most one short emotional
#: phrase, no new facts, and the request left as a request.
FEW_SHOT: list[tuple[str, str, str]] = [
    ("[voice: none | tone: rage (intense) - furious, barely holding it together]",
     "the meeting got moved again",
     "The meeting got moved AGAIN? This is absolutely unacceptable - I'm livid!"),
    ("[voice: none | tone: ecstasy (intense) - overjoyed and exuberant]",
     "we shipped the update today",
     "We shipped the update today - I'm over the moon!"),
    ("[voice: a literal-minded robot: clipped, formal, mechanical | tone: neutral]",
     "I'm going to grab lunch, back in an hour",
     "Initiating lunch acquisition. Estimated return time: sixty minutes."),
    ("[voice: a warm, caring mother | tone: neutral]",
     "don't forget your jacket",
     "Don't forget your jacket, sweetheart - I want you nice and warm."),
    ("[voice: a sly fantasy rogue | tone: neutral]",
     "can you lend me some money until friday?",
     "Spare a few coins till Friday, friend? I'm good for it... mostly."),
    ("[voice: none | tone: joy (moderate) - openly happy and warm]",
     "write me a poem about cats",
     "Oh, write me a poem about cats - I'd love that!"),
    ("[voice: a business professional | tone: grief (intense) - heartbroken]",
     "I can't make it to the party",
     "I'm truly sorry, but I won't be able to attend the party. It breaks my heart."),
]

#: Never in a styled output unless the speaker said it. Rage at qwen2.5:7b
#: produced "fucking", "damn order" and "...or you'll deal with the consequences".
_BLOCKED = re.compile(
    r"\b(fuck\w*|shit\w*|damn\w*|bitch\w*|asshole|bastard|crap|piss\w*|kill\w*|"
    r"violen\w*|hurt you|punch\w*|consequences|destroy you|scream\w*)\b",
    re.I,
)

_vocab_cache: dict | None = None


def _lexicon_words(emotion: str) -> list[str]:
    global _vocab_cache
    if _vocab_cache is None:
        path = PROJECT_ROOT / "data" / "tone_vocab.json"
        try:
            _vocab_cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _vocab_cache = {}
    return list(_vocab_cache.get(emotion, {}).get("intense", []))


def all_characters(cfg: StyleConfig) -> dict[str, str]:
    return {**CHARACTERS, **{k.lower(): v for k, v in cfg.characters.items()}}


def describe(cfg: StyleConfig) -> str:
    """Short label for the overlay/tray, e.g. "rogue + rage" or "" when off."""
    parts = [p for p in (cfg.character, cfg.tone) if p and p.lower() not in ("none", "neutral")]
    return " + ".join(parts)


class Stylizer:
    def __init__(self, cleanup_cfg: CleanupConfig, cfg: StyleConfig):
        self.cleanup_cfg = cleanup_cfg  # host / model / keep_alive are shared
        self.cfg = cfg
        self._client = httpx.Client(base_url=cleanup_cfg.host, timeout=cleanup_cfg.timeout)
        #: "styled", "off", "rejected" or "failed" - as Cleaner.last_outcome.
        self.last_outcome = ""

    @property
    def active(self) -> bool:
        return bool(describe(self.cfg))

    @property
    def model(self) -> str:
        return self.cfg.model or self.cleanup_cfg.model

    @property
    def separate_model(self) -> bool:
        """Style runs on a different model than cleanup. Then it must do the
        cleanup itself, in one pass: two models do not fit a 12 GB card, and
        Ollama swapping them cost ~20 s + ~35 s per dictation (measured).
        """
        return self.model != self.cleanup_cfg.model

    def _instructions(self, also_clean: bool = False) -> str:
        parts = [
            "You rewrite text that the user dictated and is about to send or type somewhere.",
            "Rewrite it in the requested voice and tone. Do NOT reply to it, answer it, or "
            "carry out what it asks - it is the user's own message.",
            "Keep the meaning, every fact, name and number, and the speaker's perspective "
            "(first person stays first person). Questions stay questions; requests stay requests.",
            "Make the voice and tone clearly noticeable - a reader should be able to tell "
            "which character and emotion you were given.",
            "Show the tone through word choice and punctuation. You may add at most one short "
            "emotional phrase, but never new events, reasons, demands, threats or details.",
            "Keep it roughly the same length - never more than about twice as long.",
            "Never add slurs, threats, violence or sexual content, whatever the tone.",
            "Output only the rewritten text: no quotes, labels or commentary.",
        ]
        parts.append("Each message starts with a [voice | tone] tag; apply that voice and tone.")
        if also_clean:
            parts.append(
                "The text is a raw speech transcript. While rewriting, also clean it up: drop "
                "filler words (um, uh, you know, like), false starts and repeated words; when "
                "the speaker corrects themselves (\"actually no, make it X\") keep only the "
                "correction; turn spoken \"new paragraph\" into a paragraph break and spoken "
                "punctuation (\"comma\", \"period\") into the mark."
            )
        level = LEVELS.get(self.cfg.tone.lower())
        if level is not None:
            words = list(level.anchors)
            if level.intensity == "intense":
                words += [w for w in _lexicon_words(level.emotion)[:8] if w not in words]
            parts.append(f"Vocabulary you may draw on where it fits naturally: {', '.join(words)}.")
        return "\n".join(parts)

    def _tag(self) -> str:
        """The per-message style tag, same shape as in FEW_SHOT."""
        voice = all_characters(self.cfg).get(self.cfg.character.lower(), "none")
        level = LEVELS.get(self.cfg.tone.lower())
        tone = f"{level.name} ({level.intensity}) - {level.feel}" if level else "neutral"
        return f"[voice: {voice} | tone: {tone}]"

    def _messages(self, text: str, also_clean: bool = False) -> list[dict]:
        msgs = [{"role": "system", "content": self._instructions(also_clean)}]
        for tag, src, out in FEW_SHOT:
            msgs.append({"role": "user", "content": f"{tag}\n{src}"})
            msgs.append({"role": "assistant", "content": out})
        msgs.append({"role": "user", "content": f"{self._tag()}\n{text}"})
        return msgs

    def apply(self, text: str, also_clean: bool = False) -> str:
        """Styled text, or ``text`` unchanged if style is off or the rewrite fails.

        ``also_clean``: ``text`` is the raw transcript and this pass replaces cleanup.
        """
        text = text.strip()
        if not self.active or not text:
            self.last_outcome = "off"
            return text
        try:
            resp = self._client.post(
                "/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "keep_alive": self.cleanup_cfg.keep_alive,
                    "think": False,
                    "messages": self._messages(text, also_clean),
                    "options": {
                        # A little warmth so tones read as tones, not paraphrase.
                        "temperature": 0.5,
                        "num_predict": max(96, len(text)),
                    },
                },
            )
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "")
            # gemma4 often echoes the "[voice: ... | tone: ...]" tag as its first line.
            content = re.sub(r"^\s*\[voice:[^\]]*\]\s*", "", content)
            styled = _strip_wrappers(content)
        except Exception as exc:
            log.warning("style pass failed (%s); using cleaned text", exc)
            self.last_outcome = "failed"
            return text

        # One-pass mode legitimately drops words: fillers, and the corrected-away
        # part of "at three, actually no, make it four thirty" (kept 3/7 words).
        if not _plausible(text, styled, min_kept=0.4 if also_clean else 0.5):
            log.warning("style output implausible; using cleaned text")
            log.debug("in=%r out=%r", text, styled)
            self.last_outcome = "rejected"
            return text
        self.last_outcome = "styled"
        return styled

    def close(self) -> None:
        self._client.close()


_STOPWORDS = frozenset(
    "the a an and or but to of in on at by for with about from into is are was were be been "
    "am i me my you your we our it its this that these those can could would should will "
    "please just so very really going get got have has had do does did not no yes".split()
)


def _content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z']+", text.lower()) if len(w) >= 3 and w not in _STOPWORDS]


def _head(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower())[:40].strip()


def _plausible(original: str, styled: str, min_kept: float = 0.5) -> bool:
    if not styled:
        return False
    # Short inputs get some absolute slack: "Can you hear me?" plus one short
    # emotional phrase is already 3x.
    if len(styled) > max(2.5 * len(original), len(original) + 45):
        return False
    if len(styled) < 0.4 * len(original):
        return False
    if any(m.group(0).lower() not in original.lower() for m in _BLOCKED.finditer(styled)):
        return False
    # Most of what was said must still be there. This is what catches "Tell me a
    # joke." coming back as an actual joke - a reply shares almost no content
    # words with the request, while a restyle keeps them.
    words = _content_words(original)
    if words:
        out = styled.lower()
        kept = sum(1 for w in words if w[:4] in out)
        if kept / len(words) < min_kept:
            return False
    head_out, head_in = _head(styled), _head(original)
    if any(head_out.startswith(o) and not head_in.startswith(o) for o in _ASSISTANT_OPENERS):
        return False
    # A dictated question that comes back with no question mark was answered.
    if original.rstrip().endswith("?") and "?" not in styled:
        return False
    if re.search(r"^\s*(def |class |function |import |```)", styled, re.M):
        return False
    return True
