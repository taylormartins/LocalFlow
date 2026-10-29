"""Crash recovery: a dictation in progress is kept on disk and comes back.

No microphone and no models: audio is fed to the recorder, the transcriber is
fake, and the clipboard call is captured (never touched).

    uv run python tests/recovery.py
"""
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from localflow import inject, recovery  # noqa: E402
from localflow.config import Config  # noqa: E402
from localflow.engine import Engine  # noqa: E402

failures = 0


def expect(name, ok, detail=""):
    global failures
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + (f": {detail}" if not ok else ""))


tmp = Path(tempfile.mkdtemp(prefix="lf-recovery-"))
recovery.folder = lambda: tmp  # never the real data folder
clip = []
inject.set_clipboard_text = clip.append

cfg = Config()
cfg.history.enabled = False
cfg.cleanup.enabled = False
engine = Engine(cfg)
engine.transcriber = type("T", (), {"transcribe": lambda self, a: "words I was saying"})()

# a tone, so the round trip can be compared
rate = 16000
tone = (0.4 * np.sin(np.arange(rate * 3) * 0.05)).astype(np.float32)


def take(seconds=3.0):
    """Record ``seconds`` of the tone through the real Recorder."""
    assert engine.recorder.start_external(rate)
    audio_in = np.resize(tone, int(seconds * rate))
    for i in range(0, len(audio_in), 1600):
        engine.recorder.feed(audio_in[i:i + 1600])


print("a recording is copied to disk as it happens")
take(3.0)
files = recovery.pending()
expect("one take file while recording", len(files) == 1, str(files))
time.sleep(1.1)
engine.recorder.feed(tone[:1600])  # a write after a second flushes the file
expect("the file has audio before the recording ends", files and files[0].stat().st_size > 50000,
       str([f.stat().st_size for f in files]))
audio = engine.recorder.stop()
spool = engine.recorder.last_spool
expect("stop() keeps the copy and says which", spool is not None and spool.exists())
back, r = recovery.load(spool)
expect("it reads back as the same audio", r == rate and np.allclose(back[:4800], tone[:4800], atol=1e-3))

print("cancelled or delivered takes leave nothing behind")
engine._process_and_inject(audio, False, False, spool=spool)
expect("a finished dictation removes its copy", not spool.exists())
take(2.5)
engine.recorder.cancel()
expect("...but cancel() removes it", recovery.pending() == [], str(recovery.pending()))
take(2.5)
engine.recorder.stop()
kept = engine.recorder.last_spool
expect("CONTROL: a plain stop leaves the copy", kept is not None and kept.exists())

print("a pipeline that fails keeps the copy")
bad_engine_t = engine.transcriber
engine.transcriber = type("T", (), {"transcribe": lambda self, a: (_ for _ in ()).throw(RuntimeError("boom"))})()
engine._process_and_inject(tone, False, False, spool=kept)
expect("the copy is still there for the next start", kept.exists())
engine.transcriber = bad_engine_t

print("the next start recovers it")
notes = []
text = recovery.recover(engine, lambda *a: notes.append(a), recovery.pending())
expect("the words come back", text == "words I was saying", text)
expect("on the clipboard", clip == ["words I was saying"], str(clip))
expect("and a notice says so", len(notes) == 1 and "Recovered" in notes[0][0], str(notes))
expect("the copy is gone afterwards", recovery.pending() == [] and not list(tmp.glob("*")),
       str(list(tmp.glob("*"))))

print("a take that cannot be recovered cannot crash-loop the start")
take(2.5)
engine.recorder.stop()
engine.transcriber = type("T", (), {"transcribe": lambda self, a: (_ for _ in ()).throw(RuntimeError("boom"))})()
recovery.recover(engine, None, recovery.pending())
expect("it is parked as .bad, not retried", recovery.pending() == [] and len(list(tmp.glob("*.bad"))) == 1,
       str(list(tmp.glob("*"))))
for f in tmp.glob("*.bad"):
    f.unlink()
engine.transcriber = bad_engine_t

print("short and stale takes are dropped quietly")
engine.recorder.start_external(rate)
engine.recorder.feed(tone[:8000])  # half a second
engine.recorder.stop()
notes.clear()
recovery.recover(engine, lambda *a: notes.append(a), recovery.pending())
expect("under two seconds: removed, no notice", recovery.pending() == [] and not notes, str(notes))

for f in tmp.glob("*"):
    f.unlink()
tmp.rmdir()
sys.exit(1 if failures else 0)
