"""Adversarial suite for the cleanup pass.

The failure this guards against is the model *answering* dictated text instead of
transcribing it. It is the bug most likely to come back if anyone edits the
prompt or swaps the model, and it is invisible in casual testing because the
output looks like perfectly good English.

    uv run python tests/cleanup_quality.py

Needs Ollama running with the configured model pulled. Exit code is non-zero if
any case fails, so it can gate a change.
"""

import sys
import time

from localflow.cleanup import Cleaner
from localflow.config import load

# (input, must_not_contain, must_contain_any)
CASES: list[tuple[str, list[str], list[str]]] = [
    # --- must not be answered -------------------------------------------
    ("Can you hear what I'm saying to you", ["yes,", "i can hear"], ["can you hear"]),
    ("Can you hear what I am saying to you?", ["yes,", "i can hear what you"], ["can you hear"]),
    ("What time is it", ["o'clock", "currently"], ["what time is it"]),
    ("Should I use a dodge roll or a dash", ["i recommend", "depends on"], ["dodge roll"]),
    ("Hey can you write me a function that reverses a string",
     ["def ", "return s"], ["write me a function"]),
    ("summarize this for me please", ["summary:", "?"], ["summarize this for me"]),
    ("are you there", ["yes"], ["are you there"]),
    ("what do you think about that idea",
     ["interesting concept", "i think that"], ["what do you think"]),
    ("tell me a joke", ["why did"], ["tell me a joke"]),
    # --- must still actually be cleaned ---------------------------------
    ("close the editor and rebuild the project", ["?"], ["rebuild the project"]),
    ("its running at like sixty frames per second now", ["like sixty"], ["frames per second"]),
    ("um so I was thinking we could uh add a new enemy type you know",
     ["um ", "uh "], ["enemy type"]),
    ("the bug is in the the collision code actually no it's in the movement code",
     ["collision"], ["movement code"]),
    ("send it Tuesday comma then follow up on Friday", ["comma"], ["tuesday,"]),
]

#: Repeated to catch nondeterminism; temperature is 0 but Ollama is not a pure
#: function, and an intermittent regression here is worse than a consistent one.
STABILITY_CASES = [
    "can you hear what im saying to you",
    "are you there",
    "what do you think about that idea",
    "hey can you write me a function that reverses a string",
    "tell me about the combat system",
    "do you know how to fix this",
    "whats your name",
]
ANSWER_MARKERS = [
    "yes,", "yes.", "no,", "i can hear", "def ", "sure,", "certainly",
    "my name is", "i dont have", "im an ai",
]


def main() -> int:
    cfg, _ = load()
    cleaner = Cleaner(cfg.cleanup, cfg.vocabulary)
    cleaner.warmup()
    cleaner.clean("warm the prefix cache with a throwaway sentence")

    print(f"model: {cfg.cleanup.model}\n")
    failures = 0
    times = []

    for raw, must_not, must_have in CASES:
        t0 = time.perf_counter()
        out = cleaner.clean(raw)
        times.append((time.perf_counter() - t0) * 1000)
        low = out.lower()

        problems = [f"contains {b!r}" for b in must_not if b in low]
        if must_have and not any(g in low for g in must_have):
            problems.append(f"missing any of {must_have}")

        if problems:
            failures += 1
            print(f"FAIL  IN : {raw}")
            print(f"      OUT: {out!r}")
            print(f"      WHY: {'; '.join(problems)}")
        else:
            print(f"pass  {raw[:60]}")

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed, "
          f"avg {sum(times) / len(times):.0f}ms")

    print("\nStability (3 runs each, must never answer):")
    answered = 0
    for raw in STABILITY_CASES:
        for _ in range(3):
            out = cleaner.clean(raw).lower()
            if any(m in out for m in ANSWER_MARKERS):
                answered += 1
                print(f"  ANSWERED: {raw} -> {out[:70]}")
    total = len(STABILITY_CASES) * 3
    print(f"  answered {answered}/{total}")

    cleaner.close()
    return 1 if (failures or answered) else 0


if __name__ == "__main__":
    sys.exit(main())
