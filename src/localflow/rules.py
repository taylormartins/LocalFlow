"""Algorithmic cleanup: tidy a transcript with rules, no LLM.

The LLM pass (cleanup.py) needs Ollama, a model, and a GPU with room to spare.
This needs nothing, runs in a few milliseconds, and cannot answer your
question instead of transcribing it, because it never writes words of its own.
It only deletes, punctuates and recases.

What it handles, in pipeline order:

1. Spoken commands: "new line", "new paragraph", "comma", "period", "question
   mark", "scratch that"...
2. Self-corrections with an explicit marker: "Tuesday, no wait, Wednesday" and
   "it's in the collision code, actually no, the movement code".
3. Filler: um / uh / er / hmm always; "you know", "I mean", "like" only where
   they are plainly filler (set off by commas, or "at like sixty").
4. Stutters: "the the", "I think I think", "w- we".
5. Casing and punctuation: sentence case, "i" -> "I", contractions missing their
   apostrophe, glossary terms spelled as configured, a closing full stop or
   question mark.

Where a rule would have to guess, it leaves the words alone. An untidy word is
cheap; a deleted one is lost. What it cannot do is the LLM's real strength:
rephrasing a sentence that only makes sense once the correction is understood.
Use tests/rules_quality.py to check any change here.
"""

from __future__ import annotations

import re

# -- word lists ------------------------------------------------------------

#: Always filler, wherever they appear.
#: "Mm-hmm" is left alone: it usually means yes.
_FILLER_SOUNDS = r"(?<!-)(?:u+m+|u+h+m*|e+r+m+|e+r+|a+h+|h+m+)(?!-|\s+huh)"

#: Words where a doubled form is real English ("I had had enough", "that that
#: is", "is is" is not, but "it is, is it" crosses punctuation and never matches).
_LEGIT_DOUBLES = {"had", "that", "very", "so", "bye", "no", "yeah", "ha", "really", "far", "now", "go"}

#: "like" before a number after one of these means "about": "at like sixty".
_LIKE_APPROX_AFTER = {
    "at", "for", "in", "on", "around", "about", "of", "after", "within", "under",
    "over", "from", "takes", "took", "is", "was", "be", "been", "maybe",
}

_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen"
    "|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty"
    "|seventy|eighty|ninety|hundred|thousand|million|billion|a\\s+(?:few|couple|dozen|hundred|thousand)"
    "|half|\\d"
)

#: Explicit "I got that wrong, here's the right one" markers. Bare "actually" or
#: "no" are deliberately absent - both are ordinary words far more often.
_CORRECTION_MARKERS = (
    r"no\s+wait", r"wait\s+no", r"actually\s+no", r"no\s+actually", r"actually\s+wait",
    r"no\s+sorry", r"sorry\s+no", r"sorry\s+I\s+mean", r"I\s+mean", r"I\s+meant",
    r"or\s+rather", r"correction",
)

#: "scratch that" deletes what came just before it.
_DELETE_MARKERS = r"(?:scratch\s+that|delete\s+that|strike\s+that|forget\s+that|never\s*mind\s+that)"

#: Opening words that make an unpunctuated sentence a question.
_QUESTION_STARTS = {
    "who", "what", "when", "where", "why", "how", "which", "whose", "whom",
    "can", "could", "would", "should", "will", "shall", "do", "does", "did",
    "is", "are", "was", "were", "am", "have", "has", "had", "may", "might",
    "isn't", "aren't", "don't", "doesn't", "didn't", "won't", "wouldn't",
    "can't", "couldn't", "shouldn't", "haven't", "hasn't", "wasn't", "weren't",
}

#: Apostrophe-less contractions Whisper sometimes emits. Only unambiguous ones:
#: "its", "were", "well", "ill", "id", "hell", "shed", "lets", "wed" are all real
#: words too, so they are left as spoken.
_CONTRACTIONS = {
    "im": "I'm", "ive": "I've", "dont": "don't", "doesnt": "doesn't", "didnt": "didn't",
    "cant": "can't", "couldnt": "couldn't", "shouldnt": "shouldn't", "wouldnt": "wouldn't",
    "isnt": "isn't", "arent": "aren't", "wasnt": "wasn't", "werent": "weren't",
    "havent": "haven't", "hasnt": "hasn't", "hadnt": "hadn't", "wont": "won't",
    "youre": "you're", "youve": "you've", "youll": "you'll", "theyre": "they're",
    "theyve": "they've", "theyll": "they'll", "thats": "that's", "whats": "what's",
    "theres": "there's", "heres": "here's", "wheres": "where's", "whos": "who's",
    "hows": "how's", "aint": "ain't", "shouldve": "should've", "wouldve": "would've",
    "couldve": "could've", "mustnt": "mustn't", "neednt": "needn't", "itll": "it'll",
    "thatll": "that'll", "weve": "we've",
}

_SENTENCE_END = ".!?"


# -- step 1: spoken commands -------------------------------------------------

#: Whisper writes these out ("comma") or half-punctuates them ("comma,").
_SPOKEN_PUNCT = [
    (r"exclamation\s+(?:mark|point)", "!"),
    (r"question\s+mark", "?"),
    (r"full\s+stop", "."),
    (r"semi-?\s?colon", ";"),
    (r"comma", ","),
    (r"colon", ":"),
]

#: "period" is often a noun ("trial period"), so it only counts as punctuation
#: at the end of a clause and not after a word that makes it a noun.
_PERIOD_NOUN_BEFORE = {
    "a", "the", "this", "that", "each", "per", "grace", "trial", "waiting", "time",
    "same", "whole", "short", "long", "rest", "test", "free", "notice", "cooling",
    "honeymoon", "transition", "billing", "reporting", "probation", "probationary",
    "my", "your", "her", "his", "their", "our", "its", "one", "first", "last", "next",
}


#: After one of these, a punctuation word is a noun: "put a comma after it".
_DETERMINERS = {
    "a", "an", "the", "this", "that", "no", "one", "another", "each", "every",
    "extra", "missing", "any", "your", "my", "of", "use", "add",
}


def _sub_unless_noun(text: str, spoken: str, mark: str) -> str:
    # Swallow punctuation Whisper put around the spoken word: "Tuesday, comma, then".
    pattern = re.compile(rf"(?P<prev>[\w']+)?(?P<gap>[,.]?\s*)\b{spoken}\b[,.]?", re.I)

    def sub(m: re.Match[str]) -> str:
        prev = m.group("prev") or ""
        if prev.lower() in _DETERMINERS:
            return m.group(0)
        return prev + mark

    return pattern.sub(sub, text)


def _apply_commands(text: str) -> str:
    for spoken, mark in _SPOKEN_PUNCT:
        text = _sub_unless_noun(text, spoken, mark)

    def period(m: re.Match[str]) -> str:
        before = m.group(1)
        if before.lower() in _PERIOD_NOUN_BEFORE:
            return m.group(0)
        return before + "."

    # "...the end period" / "...the end, period." / "...the end period New..."
    text = re.sub(
        r"\b([\w']+)[,]?\s+period\b[.,]?(?=\s*(?:$|[A-Z\n]|new\s+(?:line|paragraph)))",
        period,
        text,
        flags=re.I,
    )
    text = re.sub(r"\bopen\s+(?:quote|quotes|quotation\s+marks?)\b[,.]?\s*", '"', text, flags=re.I)
    text = re.sub(r"[,.]?\s*\b(?:close\s+quotes?|end\s+quotes?|unquote)\b,?", '"', text, flags=re.I)
    # Paragraph before line, so "new paragraph" is not read as "new" + "paragraph".
    # Punctuation before the command stays: "Dear Sam, new line" keeps its comma.
    text = re.sub(r"\s*\bnew\s+paragraph\b[,.]?\s*", "\n\n", text, flags=re.I)
    text = re.sub(r"\s*\b(?:new\s+line|next\s+line|line\s+break)\b[,.]?\s*", "\n", text, flags=re.I)
    return text


# -- step 2: self-corrections -------------------------------------------------

_WORD = re.compile(r"[\w']+")


def _norm(word: str) -> str:
    return word.lower().strip("'")


def _repair(before: str, after: str) -> str:
    """Merge "<before> <marker> <after>" into what the speaker settled on.

    ``after`` replaces the tail of ``before``. Where that tail starts is found by
    anchoring on a word the two share, lined up position for position:

    - "at 3 PM" + "4 PM": "PM" lines up, so "4" replaces "3" -> "at 4 PM".
    - "the bug is in the collision code" + "it's in the movement code": "in"
      lines up, and "it's" ahead of it only restarts the sentence, so it goes
      -> "the bug is in the movement code".

    With no shared word, a short correction swaps out as many words as it has
    ("the red car" + "blue truck"); a longer one is taken to be one replacement
    word plus the rest of the sentence ("use react" + "vue for this").
    """
    b_words = before.split()
    a_words = after.split()
    while b_words and _norm(_bare(b_words[-1])) in _INTERJECTIONS:
        b_words.pop()  # "at 3 PM, sorry" - the "sorry" is not content
    if not a_words:
        return " ".join(b_words)
    if not b_words:
        return after

    # Anchor on the first of after's first three words found in before - the
    # last such occurrence, since a correction revises what was said most recently.
    for skip, word in enumerate(a_words[:3]):
        key = _norm(_bare(word))
        if not key:
            continue
        for i in range(len(b_words) - 1, -1, -1):
            if _norm(_bare(b_words[i])) != key:
                continue
            # Where the corrected span ends: at after's last word, if before has
            # it too. "whether the server is up" + "the database server" only
            # replaces "the server", and "is up" stays.
            last = _norm(_bare(a_words[-1]))
            tail = next(
                (b_words[j + 1 :] for j in range(i, len(b_words)) if _norm(_bare(b_words[j])) == last),
                [],
            )
            lead = a_words[:skip]
            if all(_norm(_bare(w)) in _RESTART_WORDS for w in lead):
                # "it's in the..." - the lead words restate the subject; drop them.
                return " ".join(b_words[:i] + a_words[skip:] + tail)
            # "4 PM" against "3 PM" - the lead words replace the ones in front.
            return " ".join(b_words[: max(i - skip, 0)] + a_words + tail)

    if len(a_words) <= 2:
        keep = max(len(b_words) - len(a_words), 1 if len(b_words) > 1 else 0)
    else:
        keep = len(b_words) - 1
    return " ".join(b_words[:keep] + a_words)


def _bare(word: str) -> str:
    """The word without punctuation stuck to it: "PM," -> "PM"."""
    m = _WORD.search(word)
    return m.group(0) if m else ""


#: Lead words of a correction that only restate the subject: "it's in the..."
_RESTART_WORDS = {
    "it's", "its", "it", "it is", "that's", "that", "there's", "he's", "she's",
    "they're", "we're", "i'm", "you're", "this", "is", "was", "are", "were",
    "he", "she", "they", "we", "i", "you",
}


#: Words that can sit in front of a marker without being the thing corrected:
#: "Sorry, I mean it" and "Oh, no wait" have nothing to repair.
_INTERJECTIONS = {"sorry", "no", "oh", "well", "okay", "ok", "so", "yeah", "yes", "um", "uh"}

#: "I mean it", "I mean that", "I mean well": emphasis, not a correction.
_NOT_AFTER_I_MEAN = {"it", "that", "this", "well", "what", "business", "to", "no", "you", "nothing"}


def _apply_corrections(text: str) -> str:
    markers = "|".join(_CORRECTION_MARKERS)
    # Markers need to be set off from the words around them. "I mean" inside
    # "what I mean is" is not a correction, so it must follow a comma or open
    # the sentence. The correction itself runs to the next comma or sentence
    # end; whatever follows a comma ("..., so plan for that") is kept as is.
    pattern = re.compile(
        rf"(?P<pre>^|[,.!?]|\s)\s*\b(?P<marker>{markers})\b[,.]?\s+(?P<after>[^,.!?\n]+)",
        re.I,
    )
    pos = 0
    for _ in range(6):  # a few corrections per dictation at most
        m = pattern.search(text, pos)
        if not m:
            break
        marker = re.sub(r"\s+", " ", m.group("marker").lower())
        after = m.group("after").strip()
        first_after = _norm(after.split()[0]) if after.split() else ""
        if marker in ("i mean", "i meant") and (
            m.group("pre").strip() == "" or first_after in _NOT_AFTER_I_MEAN
        ):
            pos = m.end("marker")
            continue
        # The part being corrected: back to the start of the sentence.
        start = max(text.rfind(c, 0, m.start("marker")) for c in ".!?\n") + 1
        before = text[start : m.start("marker")].strip().rstrip(",")
        if before and all(_norm(w) in _INTERJECTIONS for w in _WORD.findall(before)):
            pos = m.end("marker")
            continue
        if not before and start > 0 and text[start - 1] != "\n":
            # "Book it for Monday. Actually no, Tuesday." - the marker opens a
            # sentence, so it corrects the one before.
            prev_start = max(text.rfind(c, 0, start - 1) for c in ".!?\n") + 1
            prev = text[prev_start : start - 1].strip()
            if prev:
                lead = text[:prev_start] + (" " if prev_start and text[prev_start - 1] != "\n" else "")
                text = lead + _repair(prev, after) + text[start - 1] + text[m.end() :]
                pos = len(lead)
                continue
        fixed = _repair(before, after) if before else after
        lead = text[:start] + (" " if start and text[start - 1] != "\n" else "")
        text = lead + fixed + text[m.end() :]
        pos = len(lead)
    # Delete markers: drop everything back to the previous sentence boundary,
    # or the previous sentence if the marker opens one.
    del_pat = re.compile(rf"[,.]?\s*\b{_DELETE_MARKERS}\b[,.!]?", re.I)
    for _ in range(4):
        m = del_pat.search(text)
        if not m:
            break
        head = text[: m.start()].rstrip()
        cut = max(head.rfind(c) for c in ".!?\n")
        if cut == len(head) - 1 and cut >= 0:
            # "First sentence. Scratch that." -> remove the whole previous sentence.
            cut = max(head[:cut].rfind(c) for c in ".!?\n")
        text = (head[: cut + 1] if cut >= 0 else "") + text[m.end() :]
    return text


# -- step 3: filler -----------------------------------------------------------


_OPENERS = r"(?:so|okay|ok|alright|right|well)"


def _remove_filler(text: str) -> str:
    # A dictation opening "So, um, ..." or "um so ...": the opener is filler too.
    text = re.sub(
        rf"^\s*(?:{_OPENERS}\s*,?\s+(?={_FILLER_SOUNDS}\b)|{_FILLER_SOUNDS}\s*,?\s+{_OPENERS}\b,?\s*)",
        "",
        text,
        flags=re.I,
    )
    # Hesitation sounds, with the comma Whisper hangs on them: "So, um, I think".
    text = re.sub(rf"(?:,\s*)?\b{_FILLER_SOUNDS}\b[,.]?(?:\s*-+)?", "", text, flags=re.I)
    # Comma-bracketed discourse fillers: ", you know," / ", I mean," / ", like,".
    for phrase in (r"you\s+know", r"I\s+mean", r"like", r"basically", r"sort\s+of", r"kind\s+of", r"you\s+see"):
        text = re.sub(rf",\s*{phrase}\s*,", ",", text, flags=re.I)
        # Sentence-final: "that's the plan, you know." -> "that's the plan."
        if phrase in (r"you\s+know", r"like", r"I\s+mean"):
            text = re.sub(rf",\s*{phrase}\s*(?=[.!?]|$)", "", text, flags=re.I)
    # Sentence-initial: "You know, I think..." / "Like, it's..." / "I mean, ..."
    text = re.sub(
        r"(^|[.!?\n]\s*)(?:you\s+know|like|I\s+mean|so\s+like|okay\s+so|so\s+yeah|well\s+so),\s*",
        r"\1",
        text,
        flags=re.I,
    )
    # A dictation opening with a bare "So," - the LLM drops it too. Not "Okay,"
    # or "Right,": in a reply those carry meaning.
    text = re.sub(r"^\s*so\s*,\s+", "", text, flags=re.I)
    # "at like sixty" -> "at sixty": approximation "like" before a number.
    after = "|".join(sorted(_LIKE_APPROX_AFTER))
    text = re.sub(rf"\b({after})\s+like\s+(?=(?:{_NUMBER_WORDS}))", r"\1 ", text, flags=re.I)
    # Trailing "you know" with no comma at the very end.
    text = re.sub(r"\s+you\s+know\s*([.!?]?)\s*$", r"\1", text, flags=re.I)
    return text


# -- step 4: stutters ----------------------------------------------------------


def _remove_stutters(text: str) -> str:
    # Cut-off word fragments: "w- we", "wh- what", "I- I".
    text = re.sub(
        r"\b(\w{1,4})-\s+(?=(\w+))",
        lambda m: "" if m.group(2).lower().startswith(m.group(1).lower()) else m.group(0),
        text,
    )

    # Repeated phrases of up to four words: "the the" / "we need to we need to".
    # Multi-word repeats may straddle a comma ("I think, I think we should");
    # single words may not, or "That is, is it not" loses a word.
    def dedupe(m: re.Match[str]) -> str:
        first = m.group(1)
        words = first.split()
        if len(words) == 1 and words[0].lower() in _LEGIT_DOUBLES:
            return m.group(0)
        return first

    for n in (4, 3, 2, 1):
        phrase = r"[\w']+" + r"(?:\s+[\w']+)" * (n - 1)
        sep = r",?\s+" if n > 1 else r"\s+"
        text = re.sub(rf"\b({phrase})(?:{sep}\1\b)+", dedupe, text, flags=re.I)
    return text


# -- step 5: casing and punctuation -------------------------------------------


def _fix_contractions(text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        word = m.group(0)
        fixed = _CONTRACTIONS.get(word.lower())
        if not fixed:
            return word
        if word[0].isupper() and fixed[0] != "I":
            fixed = fixed[0].upper() + fixed[1:]
        return fixed

    text = re.sub(r"\b[A-Za-z]+\b", sub, text)
    # Days and months are always proper nouns ("may" and "march" are verbs too).
    text = re.sub(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february"
        r"|april|june|july|september|october|november|december)\b",
        lambda m: m.group(1).capitalize(),
        text,
    )
    # Lone lowercase "i", and "i'm", "i'll", "i'd", "i've".
    return re.sub(r"\bi\b(?=$|[\s,.!?;:']|'[a-z])", "I", text)


def _apply_terms(text: str, terms: list[str]) -> str:
    """Spell glossary terms exactly as configured: "vsync" -> "VSync"."""
    for term in sorted(terms, key=len, reverse=True):
        pattern = re.compile(rf"(?<![\w-]){re.escape(term)}(?![\w-])", re.I)
        text = pattern.sub(term, text)
    return text


def _tidy_punctuation(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)          # "word ," -> "word,"
    text = re.sub(r",\s*([.!?;:])", r"\1", text)          # ",." -> "."
    text = re.sub(r"([.!?;:])\s*,", r"\1", text)          # ".," -> "."
    text = re.sub(r",(\s*,)+", ",", text)                 # ",," -> ","
    text = re.sub(r"([!?])\.+", r"\1", text)              # "?." -> "?"
    text = re.sub(r"\.{2}(?!\.)", ".", text)              # ".." -> "." (keep "...")
    text = re.sub(r"([,;:])(?=[^\s\d\"'])", r"\1 ", text)   # "a,b" -> "a, b" (not 1,000)
    text = re.sub(r"(^|\n)[ ,;:.]+", r"\1", text)          # no line opens with punctuation
    text = re.sub(r"([.!?])(?=[A-Za-z])", r"\1 ", text)   # "end.Next" -> "end. Next"
    return text.strip(" ,;:")


def _is_question(sentence: str) -> bool:
    words = _WORD.findall(sentence.lower())
    if not words:
        return False
    first = words[0]
    # "Hey, can you..." / "So what do you think"
    if first in ("hey", "so", "and", "but", "okay", "ok", "also", "then", "now") and len(words) > 1:
        first = words[1]
    return first in _QUESTION_STARTS and len(words) > 1


def _sentence_case(text: str) -> str:
    def cap(m: re.Match[str]) -> str:
        return m.group(1) + m.group(2).upper()

    text = re.sub(r"(^|[.!?]\s+|\n\s*|[.!?]\"\s+)([a-z])", cap, text)
    return text


def _terminate(text: str) -> str:
    """Close each paragraph's last sentence with . or ?."""
    out = []
    for line in text.split("\n"):
        stripped = line.rstrip()
        if stripped and stripped[-1] not in _SENTENCE_END + "\"':;)," and re.search(r"\w", stripped):
            last = re.split(r"(?<=[.!?])\s+", stripped)[-1]
            stripped += "?" if _is_question(last) else "."
        out.append(stripped)
    return "\n".join(out)


# -- entry point --------------------------------------------------------------


def rule_clean(text: str, terms: list[str] | None = None) -> str:
    """Clean a raw transcript without an LLM. Never adds a word."""
    text = text.strip()
    if not text:
        return text
    text = _apply_commands(text)
    text = _remove_filler(text)  # first, so "um" never splits a stutter or marker
    text = _apply_corrections(text)
    text = _remove_stutters(text)
    text = _remove_filler(text)  # a stutter can leave "you know, you know,"
    text = _tidy_punctuation(text)
    text = _fix_contractions(text)
    if terms:
        text = _apply_terms(text, terms)
    text = _sentence_case(text)
    text = _terminate(text)
    return text
