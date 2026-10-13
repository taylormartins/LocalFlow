"""Build data/tone_vocab.json from the NRC Emotion Intensity Lexicon.

For each of Plutchik's 24 emotion levels (8 emotions x mild/basic/intense), pick
words the style pass can offer the LLM as a vocabulary palette:

- intensity inside that level's band for the emotion,
- that emotion is the word's strongest one (so "anger" words don't read as fear),
- common English (wordfreq Zipf >= MIN_ZIPF) - the raw lexicon is full of rarities,
- shaped like an adjective/adverb/participle (EXPRESSIVE). NRC scores
  *association*, so frequent nouns like "lawyer", "government" or "malaria"
  score high for disgust/fear without being tone words at all,
- not in BLOCKED - the intense end of anger/fear/disgust is dominated by
  violence, sexual crime and disease terms that a rewrite must never inject.

The lexicon is NOT redistributable, so neither it nor the derived JSON is
committed (data/ is gitignored). Fetch and build locally:

    uv run python tools/build_tone_vocab.py

Lexicon: Saif M. Mohammad, "Word Affect Intensities", LREC 2018, National
Research Council Canada. Non-commercial research/educational use only.
https://saifmohammad.com/WebPages/AffectIntensity.htm
"""

from __future__ import annotations

import json
import re
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

from wordfreq import zipf_frequency

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ZIP = DATA / "NRC-Emotion-Intensity-Lexicon.zip"
URL = "https://saifmohammad.com/WebDocs/Lexicons/NRC-Emotion-Intensity-Lexicon.zip"
MEMBER = "NRC-Emotion-Intensity-Lexicon/NRC-Emotion-Intensity-Lexicon-v1.txt"
OUT = DATA / "tone_vocab.json"

#: Intensity bands for Plutchik's three rings, outer (mild) to inner (intense).
#: Only the stronger rings: below ~0.5 the lexicon is association noise
#: ("university", "county"), so mild levels rely on the hand-written anchors in
#: localflow/style.py instead.
BANDS = {"basic": (0.50, 0.72), "intense": (0.72, 1.01)}
MIN_ZIPF = 3.0
PER_LEVEL = 10

BLOCKED = re.compile(
    r"rape|molest|incest|pedo|porn|whore|slut|cannibal|terror|suicid|massacre|holocaust|"
    r"genocid|murder|kill|homicid|mutilat|tortur|slaughter|bomb|behead|lynch|nazi|slave|"
    r"gonorrh|syphil|cancer|tumor|leukemia|aids|hiv|euthan|abort|corpse|decompos|feces|"
    r"vomit|diarrh|nigg|fag|retard|kike|spic|chink|tranny|gun|shoot|stab|weapon|war\b|"
    r"assassin|bloodshed|carnage|atrocit|disease|plague|virus|infect|rot\b|maggot|cadaver|"
    r"bitch|asshole|shit|fuck|crap|damn|bastard|obese|toxic|criminal|prison|surgery|clinical|"
    r"military|police|government|political|socialist|lawyer|court|kidnap|robber|lepros|fecal|"
    r"septic|adulter|poison|homeless|funeral|bloody|deadly|armed|wound|injur|hanging|terminal|"
    r"boxing|martial|battery|defendant|povert|steal|buried|memorial|obes|fatty|contaminat|"
    r"pervert|derang|accused|illegal|punish|isolat|dying|died|crush|destroy|beating|attacking"
)
#: Descriptive word shapes: furious, delighted, terrified, hopeful, eagerly...
EXPRESSIVE = re.compile(r"(ful|ous|ive|ed|ing|less|able|ible|ly|ish|ant|ent|ic|al|y)$")


def load() -> dict[str, dict[str, float]]:
    DATA.mkdir(exist_ok=True)
    if not ZIP.exists():
        print(f"downloading {URL}")
        urllib.request.urlretrieve(URL, ZIP)
    words: dict[str, dict[str, float]] = defaultdict(dict)
    with zipfile.ZipFile(ZIP) as z:
        for line in z.read(MEMBER).decode("utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                try:
                    words[parts[0]][parts[1]] = float(parts[2])
                except ValueError:
                    pass  # header row
    return words


def build() -> dict[str, dict[str, list[str]]]:
    words = load()
    vocab: dict[str, dict[str, list[str]]] = defaultdict(dict)
    emotions = sorted({e for scores in words.values() for e in scores})
    for emotion in emotions:
        for band, (lo, hi) in BANDS.items():
            picks = []
            for word, scores in words.items():
                s = scores.get(emotion)
                if s is None or not lo <= s < hi:
                    continue
                if max(scores.values()) > s:  # belongs more to another emotion
                    continue
                if not word.isalpha() or len(word) < 4 or BLOCKED.search(word):
                    continue
                if not EXPRESSIVE.search(word):
                    continue
                zipf = zipf_frequency(word, "en")
                if zipf >= MIN_ZIPF:
                    picks.append((zipf, word))
            vocab[emotion][band] = [w for _, w in sorted(picks, reverse=True)[:PER_LEVEL]]
    return vocab


if __name__ == "__main__":
    vocab = build()
    OUT.write_text(json.dumps(vocab, indent=1), encoding="utf-8")
    for emotion, bands in vocab.items():
        for band, ws in bands.items():
            print(f"{emotion:<13}{band:<8}{len(ws):3}  {', '.join(ws[:14])}")
    print(f"wrote {OUT}")
