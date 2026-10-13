"""Style pass against the live model: does each voice/tone rewrite without
answering, inventing facts or dropping them?

    uv run python tests/style_quality.py            # default sample
    uv run python tests/style_quality.py --all      # every character x 3 tones

Prints every output for eyeballing - tone quality is a judgement call - and
fails only on the mechanical checks: answered instead of rewritten, lost a
key fact, or fell back to the unstyled text.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow import config  # noqa: E402
from localflow.config import StyleConfig  # noqa: E402
from localflow.style import CHARACTERS, Stylizer  # noqa: E402

#: (dictation, words that must survive the rewrite)
INPUTS = [
    ("Can you send me the quarterly report by Friday?", ["report", "friday"]),
    ("The build is broken again and I need someone to look at the login page.", ["login"]),
    ("I'm going to be about twenty minutes late to the meeting.", ["meeting"]),
    ("Can you hear me?", []),
    ("Tell me a joke.", []),
]

SAMPLE = [
    ("none", "annoyance"), ("none", "anger"), ("none", "rage"),
    ("none", "serenity"), ("none", "grief"), ("none", "amazement"),
    ("rogue", "neutral"), ("robot", "neutral"), ("caring mother", "neutral"),
    ("professional", "annoyance"), ("rogue", "rage"), ("doctor", "apprehension"),
]


def main() -> int:
    cfg, _ = config.load()
    combos = SAMPLE
    if "--all" in sys.argv:
        combos = [(c, t) for c in CHARACTERS for t in ("neutral", "anger", "joy")]

    failures = 0
    for character, tone in combos:
        st = Stylizer(cfg.cleanup, StyleConfig(character=character, tone=tone))
        print(f"\n=== {character} + {tone}")
        for text, keep in INPUTS:
            out = st.apply(text)
            problems = []
            if st.last_outcome != "styled":
                problems.append(st.last_outcome)
            missing = [k for k in keep if k not in out.lower().replace("twenty", "20") and k not in out.lower()]
            if missing:
                problems.append(f"lost {missing}")
            if text.endswith("?") and "?" not in out:
                problems.append("question answered")
            failures += bool(problems)
            flag = "FAIL " + ", ".join(problems) if problems else "ok"
            print(f"  [{flag}] {text}\n        -> {out}")
        st.close()
    total = len(combos) * len(INPUTS)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
