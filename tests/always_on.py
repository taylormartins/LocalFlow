"""End-to-end check of always-on mode, using Windows text-to-speech as the voice.

Enrols a start and a stop phrase from TTS takes, then plays whole "conversations"
through the real listener (no mic, faster than real time) and checks:

  * the dictation between the phrases comes out, without the phrases;
  * dictation that follows the start phrase with no pause still works;
  * similar-but-different phrases never start a dictation.

TTS is one speaker with perfect consistency, so this proves the plumbing and
the matching rules, not accuracy on a real voice. `localflow listen` is the
check for that. Runs on the CPU with small.en so it doesn't fight a running
LocalFlow for the GPU. Uses a scratch phrases folder, never your own.

    uv run python tests/always_on.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow import wake  # noqa: E402
from localflow.audio import decode_upload  # noqa: E402
from localflow.config import Config  # noqa: E402

START, STOP = "Okay listen, Marigold.", "That will be all."
TMP = Path(tempfile.mkdtemp(prefix="localflow-always-on-"))


def say(text: str, rate: int = 0, voice: int = 0) -> np.ndarray:
    out = TMP / f"tts-{abs(hash((text, rate, voice)))}.wav"
    if not out.exists():
        ps = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"$v = @($s.GetInstalledVoices() | % {{ $_.VoiceInfo.Name }});"
            f"$s.SelectVoice($v[{voice} % $v.Count]); $s.Rate = {rate};"
            f"$s.SetOutputToWaveFile('{out}'); $s.Speak(\"{text}\"); $s.Dispose()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    return decode_upload(out.read_bytes())


def gap(seconds: float) -> np.ndarray:
    rng = np.random.default_rng(int(seconds * 1000))
    return rng.normal(0, 0.0008, int(seconds * 16000)).astype(np.float32)  # room hiss


def build(*parts) -> np.ndarray:
    return np.concatenate([p if isinstance(p, np.ndarray) else gap(p) for p in parts])


def main() -> int:
    wake.WAKE_DIR = TMP / "wake"
    cfg = Config()
    cfg.whisper.model, cfg.whisper.device, cfg.whisper.compute_type = "small.en", "cpu", "int8"
    cfg.cleanup.method = "rules"
    cfg.audio.boost = "none"

    # -- enrol -----------------------------------------------------------------
    det = wake.Detector(cfg.always_on.model, wake.Phrases())
    phrases = wake.Phrases()
    for which, text in (("start", START), ("stop", STOP)):
        takes = [wake.trim(say(text, rate, voice)) for rate, voice in ((0, 0), (-1, 0), (1, 0))]
        heard = [" ".join(w.text for w in det.words(t)) for t in takes]
        print(f"{which} takes heard as: {heard}")
        setattr(phrases, which, wake.make_phrase(which, text, ["Marygold"] if which == "start" else [], takes, heard))
        print(f"  voice limit {getattr(phrases, which).dtw_limit:.3f}")
    phrases.save()

    from localflow.always_on import Listener
    from localflow.engine import Engine

    engine = Engine(cfg)
    engine.transcriber.load()
    results, events = [], []
    engine.on_result(lambda r: results.append(r.text))

    def run(audio: np.ndarray) -> tuple[list[str], list[tuple[str, str]]]:
        results.clear()
        events.clear()
        lis = Listener(engine, cfg, inject=False, on_event=lambda k, t: events.append((k, t)))
        lis.run_file(audio)
        for _ in range(600):
            if not engine.recording and engine.state.value in ("idle", "error"):
                break
            time.sleep(0.05)
        time.sleep(0.3)
        if engine.recorder.external:
            # A stop phrase was missed: close the dictation so the failure
            # shows here and doesn't silently swallow the next case.
            engine.recorder.stop()
            events.append(("left-open", ""))
        return list(results), [e for e in events if e[0] != "heard"]

    failures = 0

    def expect(name: str, ok: bool, detail: str) -> None:
        nonlocal failures
        failures += not ok
        print(f"  {'pass' if ok else 'FAIL'}  {name}: {detail}")

    print("\nconversation with pauses")
    t0 = time.perf_counter()
    got, ev = run(build(
        1.5, say("I was thinking about lunch today."), 1.0,
        say(START), 0.6, say("Send an email to Sam about the budget meeting on Friday."), 0.8,
        say(STOP), 2.0,
    ))
    text = got[0] if got else ""
    expect("one dictation", len(got) == 1, f"{got} events={ev}")
    expect("content kept", "budget meeting" in text.lower() and "sam" in text.lower(), repr(text))
    expect("phrases cut", "marigold" not in text.lower() and "be all" not in text.lower(), repr(text))
    print(f"  ({time.perf_counter() - t0:.1f}s for {13:.0f}s-ish of audio)")

    print("\nno pause after the start phrase")
    got, ev = run(build(1.0, say(START + " Remind me to call the dentist tomorrow."), 0.8, say(STOP), 2.0))
    text = got[0] if got else ""
    expect("one dictation", len(got) == 1, f"{got} events={ev}")
    expect("content kept", "dentist" in text.lower(), repr(text))
    expect("phrases cut", "marigold" not in text.lower() and "be all" not in text.lower(), repr(text))

    print("\nsimilar phrases must not start anything")
    for sentence in (
        "Okay listen, Mary told me about it.",
        "I planted a marigold in the garden.",
        "That will be all for today, thanks.",
        "Okay, listen to the radio.",
    ):
        got, ev = run(build(1.0, say(sentence), 2.0))
        expect(repr(sentence), not ev and not got, f"events={ev} dictations={got}")

    print(f"\n{'all passed' if not failures else f'{failures} FAILED'}  (scratch: {TMP})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
