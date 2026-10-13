"""Compare cleanup models on latency and correctness.

    uv run python tests/bench_models.py                       # 1.5b / 3b / 7b
    uv run python tests/bench_models.py qwen2.5:3b llama3.1:8b

IMPORTANT: every model you benchmark stays resident in VRAM for `keep_alive`.
Benchmarking three of them alongside Whisper will fill a 12GB card and make
everything crawl - the measurements then tell you nothing. Free them afterwards:

    ollama ps
    ollama stop <model>

Findings as of 2026-08-01 (RTX 4070 Ti, nothing else on the GPU):
    qwen2.5:1.5b  ~370ms  fails to fully resolve self-corrections
    qwen2.5:3b    ~436ms  keeps filler, misses spoken "new paragraph"
    qwen2.5:7b    ~607ms  resolves everything  <- default
All three scored 0/21 on answering instead of cleaning, so model size buys
cleanup quality, not safety.
"""

import statistics
import subprocess
import sys
import time

from localflow.cleanup import Cleaner
from localflow.config import load

SAMPLES = [
    ("short + clean", "Push the branch when CI goes green."),
    ("short + messy", "so um push the the branch when CI goes green"),
    ("medium + messy",
     "okay so I want to refactor the player controller because right now it is "
     "doing um way too much you know"),
    ("long + messy",
     "so um I want to refactor the the player controller because right now it is "
     "doing way too much you know it handles input and movement and animation all "
     "in one class actually no the animation part is fine it is mainly input and "
     "movement that need splitting up"),
]

#: The dictation that exposed the quality gap between 3b and 7b: a self-correction
#: plus a spoken formatting command in one breath.
FLAGSHIP = (
    "So um, I want to, uh, add a new dash attack to the player character. "
    "Actually no, make it a dodge roll instead. It should have, like, "
    "invincibility frames at the start. New paragraph. "
    "Let me know if that breaks the combo system."
)


def main() -> int:
    models = sys.argv[1:] or ["qwen2.5:1.5b", "qwen2.5:3b", "qwen2.5:7b"]
    cfg, _ = load()

    for model in models:
        cfg.cleanup.model = model
        cleaner = Cleaner(cfg.cleanup, cfg.vocabulary)
        cleaner.warmup()
        cleaner.clean("warm the prefix cache with a throwaway sentence")

        print(f"\n=== {model} ===")
        for label, text in SAMPLES:
            runs = []
            for _ in range(3):
                t0 = time.perf_counter()
                cleaner.clean(text)
                runs.append((time.perf_counter() - t0) * 1000)
            print(f"  {label:16} {statistics.median(runs):6.0f}ms")

        print(f"  flagship output: {cleaner.clean(FLAGSHIP)}")
        cleaner.close()
        # Do not leave it pinned; see the warning at the top of this file.
        subprocess.run(["ollama", "stop", model], capture_output=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
