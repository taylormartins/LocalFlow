# LocalFlow — architecture and handover notes

For whoever maintains this next. The README covers using it; this covers how it
works, why it is built this way, and the traps that cost real debugging time.

Written 2026-08-01, against the state of the repo at that date.

---

## Shape of the thing

One pipeline, two front-ends.

```
                      ┌─────────────────────────────────┐
  F9 hotkey  ────────►│  audio.Recorder                 │
                      │    Scarlett @44.1k → soxr → 16k │
                      └──────────────┬──────────────────┘
                                     ▼
                      ┌─────────────────────────────────┐
  phone upload ──────►│  transcribe.Transcriber         │
  (webm/m4a via av)   │    faster-whisper, CUDA         │
                      └──────────────┬──────────────────┘
                                     ▼
                      ┌─────────────────────────────────┐
                      │  cleanup.Cleaner                │
                      │    Ollama, few-shot, guarded    │
                      │    or rules.rule_clean (no LLM) │
                      └──────────────┬──────────────────┘
                                     ▼
              desktop ──► inject.Injector (clipboard + Ctrl+V)
              phone   ──► JSON response
```

`engine.Engine` owns that pipeline and holds a lock around it — Whisper and the
recorder are not reentrant, so exactly one dictation runs at a time. Everything
else observes it through `on_state` / `on_result` hooks, which is how the tray
icon and the HTTP layer stay decoupled from the audio path.

| Module | Responsibility |
|---|---|
| `_cuda.py` | Puts the pip-installed CUDA DLLs on the Windows loader path. **Must import before `faster_whisper`.** |
| `config.py` | TOML → dataclasses. Unknown keys warn rather than crash. |
| `audio.py` | Capture, host-API fallback, resampling, level metering, upload decoding. |
| `transcribe.py` | faster-whisper wrapper, lazy load, CPU fallback. |
| `cleanup.py` | Ollama pass, few-shot prompt, output guards, fast-path skip. `cleanup.method = "rules"` skips Ollama entirely. |
| `rules.py` | Algorithmic cleanup: deletes, punctuates, recases, never adds a word. The fallback for every LLM failure or rejection. |
| `inject.py` | Clipboard set/restore and Ctrl+V into the focused window. |
| `hotkey.py` | Global hotkeys. All logic lives in the win32 event filter — see below. |
| `engine.py` | Orchestration, state machine, history. |
| `tray.py` | pystray icon, colour per state, elapsed-time ticker. |
| `overlay.py` | Floating status pill (Tk on its own thread). WS_EX_NOACTIVATE + click-through, so the Ctrl+V paste never lands in it. Every Win32 call has declared argtypes: without them, x64 ctypes passes HWND_TOPMOST (-1) as 0xFFFFFFFF, and SetWindowPos fails silently. |
| `server.py` | FastAPI: `/api/health`, `/api/transcribe`, serves the PWA. |
| `cli.py` | Subcommands and process wiring. |
| `pwa/` | Self-contained phone client. No build step, no dependencies. |

---

## Five things that will bite you

These are all counter-intuitive, and each one cost hours.

### 1. `suppress_event()` kills your own callbacks

pynput's `Listener.suppress_event()` **raises an exception** to signal
suppression. It unwinds out of pynput's `_convert`, which means `on_press` and
`on_release` never run for any key you suppress. Suppressing F9 silently
disabled the very handler meant to react to it.

So `hotkey.py` does all state tracking inside `win32_event_filter`, keyed on
virtual-key codes, and `on_press`/`on_release` are deliberately no-ops. The
filter runs on the keyboard hook thread where any delay stalls input system
wide, so it only does set arithmetic and pushes work to a queue drained by a
worker thread.

Two consequences worth remembering:

- Windows reports `VK_LCONTROL`; hotkey specs say `<ctrl>`. `normalise_vk()`
  collapses left/right variants onto the generic code before comparing.
- Only *trigger* keys are suppressed, and only while their modifiers are held.
  Blanket-suppressing Ctrl would break the entire keyboard.

### 2. The cleanup model wants to answer you

Dictate "can you hear me?" and a helpful assistant model replies "Yes, I can
hear you." — which then gets pasted into your document.

A system prompt does not stop this. What does, in order of importance:

1. **`FEW_SHOT` in `cleanup.py`** — worked examples of questions staying
   questions. This carries the behaviour. **Add new cases here** when you find a
   failure; that list is the intended extension point.
2. Temperature 0.
3. `_looks_like_an_answer()` — rejects output opening with an acknowledgement the
   speaker never said.
4. Length-ratio and code-block checks.

Any rejection falls back to the algorithmic cleanup (`rules.py`), which only
removes filler and fixes punctuation. You never lose words.

Two subtleties in that guard, both learned by breaking it:

- **Length cannot detect this.** "Yes, I can hear what you're saying" is 1.14×
  the length of the question that prompted it — comfortably inside any sane band.
- **Compare against filler-stripped raw text.** Cleanup legitimately turns
  "so I think…" into "I think…", which looks like a newly-introduced opener
  unless you strip leading filler from the raw side first. `ANSWER_OPENERS` and
  `LEADING_FILLERS` must not overlap, or "um yeah that works" → "Yeah, that
  works" trips the guard. There is an assertion for this in the tests.

Verify any prompt or model change with `tests/cleanup_quality.py`.

### 3. Slow cleanup is almost always VRAM contention

The few-shot prefix is **not** the cost. Ollama caches it: 672 prompt tokens
re-evaluate in ~12ms. Do not trim the examples for speed — you would lose the
guardrail and gain nothing.

Warm cost is ~125ms fixed Ollama overhead plus generation at ~90 tok/s, scaling
with output length. Measured: 235ms short, ~610ms for a long rambling dictation.

When it feels slow, something else is on the GPU. With Unreal Editor open and
three Ollama models pinned by `keep_alive`, the same call took **12–20 seconds**.
Benchmarking several models is the easiest way to do this to yourself, which is
why `tests/bench_models.py` stops each one when it finishes.

```powershell
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
ollama ps
ollama stop <model>
```

### 4. iOS Safari needs HTTPS for the microphone

`getUserMedia` is undefined in an insecure context. `http://<lan-ip>:8123` loads
the PWA fine and then the mic button does nothing — a confusing failure, so the
page checks `window.isSecureContext` up front and says so.

`tailscale serve --bg 8123` is the fix: a trusted certificate, no port
forwarding, works off your network. There is no reasonable path here without it
short of installing a self-signed CA profile on the phone.

The PWA cannot type into other iOS apps — only a native keyboard extension can,
which needs a Mac, Xcode, and a paid developer account. The flow is dictate →
Copy → paste. The HTTP API is already the right shape for a native client.

### 5. Audio devices are less reliable than they look

The Scarlett appears four times (WASAPI / DirectSound / MME / WDM-KS) and the
indices **shift across reboots** — do not persist an index, match on name.
`resolve_candidates()` returns every match ranked by host API and the recorder
walks the list with retries.

Two real failures seen:

- **A host API opens and then delivers nothing.** MME accepted the stream and
  the callback never fired once, producing 40–70 second recordings of absolutely
  nothing. The recorder now detects zero blocks, blacklists that host API for the
  session, and moves on.
- **Silence looks identical to success** unless you log levels. Every capture now
  records `peak`/`rms` and warns below `QUIET_PEAK` / `SILENCE_PEAK`. `localflow
  level` is the fast hardware check.

Also worth knowing: Windows keeps a per-device capture volume separate from the
Scarlett's physical gain knob, and it can reset. It was found at 50%.

---

### 6. The "windowless" app has a hidden console, and it can block

The venv's `pythonw.exe` is a launcher that starts the console `python.exe`, so
the tray app owns an invisible conhost that nobody reads. On 2026-10-11 a
`WriteConsoleW` to it blocked forever: the hotkey worker hung mid-log-line, F9
went dead, and the process still looked healthy. `py-spy dump --native` showed
it. When launched as `pythonw.exe`, `cli._detach_hidden_console()` now calls
`FreeConsole()` and points stdout/stderr (and fds 1/2) at NUL. Don't key that
on `IsWindowVisible(GetConsoleWindow())`: on this machine the hidden console
reports visible. It *was* visible: Windows Terminal is the default terminal here,
so the console appeared as a window, and closing it killed the app. `restart.ps1`
therefore launches the base interpreter's `pythonw.exe` (path from
`.venv\pyvenv.cfg`) with `__PYVENV_LAUNCHER__` set to the venv's pythonw. That is
the variable the venv launcher itself uses, so you get the same venv with no
console process at all. The detach in `cli.py` stays as a safety net.

### 7. Ollama updates, its models folder, and Whisper's network check

Three failures from the same reboot (2026-10-12), all outside LocalFlow's code:

- **Whisper fell back to CPU because the network was down.** faster-whisper asks
  HuggingFace whether the cached model is current on every load. Right after
  wake (VPN reconnecting) that raised `RemoteProtocolError: Server disconnected`,
  which the loader took for a CUDA failure and moved Whisper to the CPU:
  transcription went from 0.2 s to 5.6 s, and the live preview starved the CPU.
  `transcribe._open` now loads with `local_files_only=True`, and only real
  CUDA errors fall back to CPU. The live preview pauses itself if Whisper is on CPU.
- **Ollama's auto-updater installs at logon** and aborts if anything is using
  Ollama. `restart.ps1` now waits for a staged installer that is newer than the
  installed version (version from the uninstall registry key; `ollama.exe` has
  no version resource).
- **Ollama 0.40 doesn't follow a junction for its models folder.** With
  `%USERPROFILE%\.ollama` junctioned to another drive, every manifest was
  reported "bad" and the model list was empty. The app's own setting (`models`
  in `%LOCALAPPDATA%\Ollama\db.sqlite`, the "Model location" setting in its UI)
  overrides `OLLAMA_MODELS`. If your models live elsewhere, point both straight
  at the real folder.

### 8. Algorithmic cleanup must stay cautious

`rules.py` exists so LocalFlow works with no Ollama at all. Most of its rules
are easy. The ones that bite are words that are filler in one sentence and
content in the next:

- **"I mean"** is a correction after a comma ("five, I mean six") but not in
  "what I mean is" or "I mean it". Hence `_NOT_AFTER_I_MEAN`, and the comma rule.
- **"period", "comma", "colon"** are nouns after a determiner ("the trial
  period", "put a comma here"). Hence `_DETERMINERS` and `_PERIOD_NOUN_BEFORE`.
- **"like"** is only removed when comma-bracketed or before a number after a
  preposition ("at like sixty"). "I'd like two" stays.
- **Doubled words** are often real: "had had", "that that". Single-word repeats
  never straddle a comma, or "That is, is it not" loses a word.
- **Self-correction** (`_repair`) aligns the correction on a word it shares
  with what came before ("3 PM" / "4 PM"), then keeps whatever followed the
  replaced span ("the server is up, I mean the database server" keeps "is up").
  Without a shared word, it replaces as little as possible.

When in doubt, a rule leaves the words alone. `tests/rules_quality.py` pins exact
outputs (including idempotence: cleaning clean text changes nothing). Add a case
before changing a rule.

## Testing

There is no unit-test suite. These are integration scripts that need a GPU,
Ollama, and (for one) a keyboard — which is where all the real bugs lived.

```powershell
uv run localflow check                      # GPU, mic, Ollama, hotkeys, server
uv run python tests/cleanup_quality.py      # adversarial suite; gate prompt changes on this
uv run python tests/rules_quality.py        # algorithmic cleanup, exact outputs; no GPU
uv run localflow clean "text"               # see what the rules do (--llm for Ollama)
uv run python tests/bench_models.py         # latency vs quality across models
uv run python tests/hotkeys.py              # hold/toggle/cancel state machine
uv run localflow level                      # live mic meter
uv run localflow transcribe clip.wav        # pipeline without a microphone
```

`tests/hotkeys.py` synthesises real key presses — close the tray app first or two
listeners fight over F9.

To generate a test clip without speaking:

```powershell
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SetOutputToWaveFile("test.wav"); $s.Speak("um so I think we should, actually no, ship it"); $s.Dispose()
```

---

## Known issues and next steps

- Short imperatives ("tell me a joke") sometimes get a spurious question mark
  from the LLM. Cosmetic; resisted fixing because the obvious fix overfits the
  prompt to the test suite.
- Git history starts at the first public release; the development before it is
  recorded only in this document.
- `whisper.language` is pinned to English. Autodetect costs latency and misfires
  on short clips, so multilingual use needs thought rather than just unpinning.
- Text settings (hotkeys, vocabulary) are `config.toml` plus a restart; the tray
  covers models, cleanup method, style and sounds.
- `/api/health` is unauthenticated and reveals model names. Fine behind
  Tailscale, not fine if you ever expose the port. The server is off by default.
- Planned: fact-check flags (never rewrites), and an always-listening mode with
  a wake phrase.

---

## Startup

`install.ps1` registers two scheduled tasks, both running `restart.vbs` →
`restart.ps1` **elevated**: "LocalFlow" at logon and "LocalFlow resume" on wake
from sleep (Power-Troubleshooter event 1). Elevated because Windows won't deliver
keystrokes to a normal-user keyboard hook while an admin window has focus;
restart-on-wake because sleep can silently drop the hook and kill the CUDA
context. `restart.ps1` stops any old instance, makes sure Ollama is up (when it
is used at all), launches the app with no console (trap 6), and supervises it:
relaunch on crash, give up after three crashes in ten minutes.

Without admin rights, the installer falls back to `localflow install-startup`,
a `LocalFlow.lnk` in the user's Startup folder pointing at `restart.vbs`. Same
supervision, but non-elevated, so hotkeys don't reach admin windows.

Because there is no console at login, `localflow run` always writes a rotating
`localflow.log` in the project root. That file is the first place to look when it
fails to come up.
