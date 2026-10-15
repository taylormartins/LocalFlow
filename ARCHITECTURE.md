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
- **A suppressed Caps Lock can strand capitals on.** If Caps Lock was on when
  LocalFlow started, nothing could turn it off: every press was swallowed,
  including one sent from code. Keys LocalFlow injects itself now carry
  `dwExtraInfo = OWN_INPUT`, and the filter lets those through. Only our own,
  not every injected key: AutoHotkey remaps to F9 must still trigger.
  `caps_lock_off()` runs at startup and whenever Caps Lock is (re)bound. The
  tray shows "Turn Caps Lock off" while capitals are on. `_watch_caps` polls
  the state, because pystray builds its menu ahead of time, not on right-click.
- **Keys can change at runtime** (tray ▸ Dictation keys).
  `HotkeyManager.replace()` swaps the whole binding list in one assignment.
  `cli.build_bindings()` is the single place bindings are built. A toggle
  recording survives a rebind, because it's matched by group, not binding.

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

**Mic boost (`audio.boost`, `Recorder.apply_gain`).** Fixed steps are 2x, 4x
and 8x. Auto (`audio.auto_gain`) is the subtle one:

- **Level alone can't tell a weak mic from an empty room.** On the Scarlett, a
  click in an empty room peaked at 0.027, about what a weak mic's speech
  peaks at. The first version of Auto boosted that x20. So Auto boosts only
  when the peak (99.9th percentile, so one click doesn't count) is at least
  6x the quietest stretch's RMS. Steady noise peaks at about 3.3x its own RMS.
  It also never lifts that quiet floor above 0.01 RMS (about -40 dBFS).
- **Auto never cuts gain.** Whisper doesn't care about level, so there's
  nothing to gain from turning down.
- **The live meter uses `Recorder.auto_gain`**, a geometric mean of recent
  recordings' gains, because the final gain only exists once the take ends.
  `snapshot()` (live preview) measures its own window.
- **Soft limiter instead of `np.clip`**, so a boosted plosive doesn't become a
  square wave.
- **Measured:** large-v3-turbo and small.en both transcribed clean TTS speech
  at -40 and even -50 dB without a boost. The boost matters for real weak
  mics: the meter and "too quiet" checks read the level, and real noise
  sits closer to quiet speech than a synthetic clip's does.

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

### 9. Corporate networks: the model download is the only fragile step

This came from a real install behind Zscaler. With the rules cleanup there is
no LLM to fetch, so the Whisper model download is the one network step left.
It fails in three different ways, and `models.py` handles each one:

- **TLS inspection.** The proxy re-signs HTTPS with a root that is in the
  Windows store but not in certifi. huggingface_hub/httpx only trust certifi,
  so the download fails with `CERTIFICATE_VERIFY_FAILED` even after `uv` works.
  `models.download` retries once through `truststore` (the OS store) and drops
  huggingface_hub's cached HTTP client so the retry really uses it. This
  happens only after a cert failure, so working machines are unchanged.
  `install.ps1` does the same for `uv sync` (`UV_SYSTEM_CERTS=1` on retry).
- **Site blocked (HTTP 403 with an HTML page from `Server: Zscaler/...`).**
  No setting gets round that, and LocalFlow must not try. `models.diagnose`
  probes `<endpoint>/api/models/<repo>` and names the cause (proxy, cert, no
  network, timeout), because a generic "check your internet" is wrong there.
  The installer exits fetch-model with code 2, finishes everything else, and
  prints the sideload steps.
- **Sideload.** `fetch-model --zip` packs `<name>/{model.bin,...}`, and
  `import-model` (installer `-ModelZip` / `-ModelDir`) unpacks it to
  `models/<name>/`. `transcribe._open` checks `models.sideloaded()` before the
  HF cache, so a sideloaded model never touches the network.
  `whisper.hf_endpoint` sets `HF_ENDPOINT` in `cli.main` before anything
  imports huggingface_hub, which reads it only once, at import.
- **LocalFlow's own mirror.** v1.1 sideloading still needed a second PC, and
  the tester didn't have one. So `models.download` falls back to copies of
  tiny.en, base.en and small.en (MIT, each zip carries NOTICE.txt) on
  LocalFlow's GitHub, in two ways:
  - the `models-v1` release assets. Those are served from
    `release-assets.githubusercontent.com`, which some filters block
    separately from github.com;
  - hidden refs `refs/localflow/models-v1/<name>` holding the same zip split
    into chunks under 50 MB, fetched with `git fetch --depth 1` into a
    throwaway bare repo. That needs only github.com, which a working
    `git clone` has already proved reachable. Normal clones never fetch these
    refs (`refs/heads/*` and tags only).

  Both paths check the zip's SHA-256 against `GITHUB_MODELS`. To add or
  replace a model, build the zip with `fetch-model --zip`, upload it to the
  release, push the chunks to the ref, then update the hash and size there.
  Don't change a published zip in place: installed copies pin its hash.
  Each path is retried once, because a dropped connection mid-download really
  happened during testing.

### 10. Always-on mode: false triggers are the enemy

`always_on.Listener` holds its own mic stream. It resamples to 16 kHz into a
30 s `Ring` (addressed by absolute sample index) and splits speech into bursts
with an RMS-vs-adaptive-floor check per 30 ms frame. A burst ends after 0.5 s
of quiet, or is cut every 6 s with 2 s of overlap. A detector thread runs
`wake.Detector` (base.en, CPU int8, 2 threads, Silero VAD first) on each burst
for timed words, and asks whether the start phrase (when idle) or the stop
phrase (while dictating) is among them.

- **Dictation reuses the recorder.** `Recorder.start_external()` and
  `feed()` make the listener's audio look like an ordinary recording, so the
  overlay meter, live preview, cues, mic boost and `stop()` all work
  unchanged. The start cue's mute became a frame window (`_mute`), not "the
  first N seconds", because a fed recording starts pre-filled with whatever
  was said right after the start phrase.
- **No initial prompt for the detector.** Priming Whisper with the phrase
  makes it hear the phrase in noise.
- **Text match (`wake.find`) decides.** The score is the *worst* word's
  similarity under the best alignment with any spelling, and every word must
  reach 0.85. Whole-phrase letter similarity was tried first: it let one
  different word ride on three matching ones (an everyday word one letter
  off the phrase's name scored 0.93).
  - The alignment allows Whisper to split or merge a word, but only with
    identical letters (`MERGE_FLOOR` 0.95). Setup learned "mary gold" for
    "Marigold", and "okay listen, Mary **told** me" then scored 0.875 as a
    string, and 0.857 at the cleverest split ("maryg|old" vs "mary|told").
    Letters cannot separate those; requiring an exact merge can.
- **The voice can only veto.** DTW over log-mel features against the setup
  takes. As a rescue for near-miss text it let "Mary told" through, because
  it genuinely sounds like "marigold". The veto threshold is max(2.5x the
  takes' own spread, 0.2). The floor matters: three near-identical takes
  gave a limit of 0.10, and the real stop phrase cut from running speech
  measured 0.13 (word times blur the edges), so it was refused. Other
  sentences measure 0.17-0.33.
- **Spellings come from setup.** What base.en heard for each take is saved as
  a variant: it's what it will hear next time. Typed alternatives shorter
  than the phrase are spliced into it (`wake.expand`). A one-word variant
  would fire whenever the name came up in conversation.
- **Cuts.** Whisper's end time for the *last word of a burst* runs short:
  cutting there left "...gold" in the dictation as "Old." So if the phrase
  ends its burst (you paused), dictation starts at the burst's end.
  Otherwise it starts just before the next word, never more than 0.6 s after
  the phrase. The stop cut is at the stop phrase's first word -0.05 s, but
  never before the previous word's end. Anything left over
  (Whisper word times are good to about 0.1 s) is removed by
  `wake.strip_phrases` through `Engine.process(transform=...)`. It strips only
  a tail of the start phrase or a head of the stop phrase, and a lone word
  only if it's long and close.
- **Setup is a separate process** (`localflow enroll`), so its Tk window never
  shares an interpreter with the overlay's Tk. The tray pauses the listener
  while it's open, so the takes don't trigger anything.
- **Reaction time** (`tests/always_on_latency.py` feeds TTS in real time).
  It was 1.3-1.7 s from the end of a phrase to detection. Now it's 0.17 s
  with a GPU and 0.75 s on the CPU:
  - *Early check:* after 0.2 s of quiet (`PEEK`), the burst so far goes to
    the detector if it's free. Only bursts at least 80% as long as the
    shortest setup take qualify, because a pause inside the phrase ("Listen
    to me, ...") otherwise spent the CPU on half a phrase.
  - *`_consumed`:* stops an early check and the full burst triggering twice.
  - *`temperature=0.0` in `Detector.words`:* a cut-off phrase looks
    uncertain, and Whisper's fallback decoded it six times. That was 6.7 s
    instead of 1.3 s, and it hid behind everything else.
  - *Detector on the GPU* when the dictation model is (`models.small_whisper`,
    falls back to CPU).
  - *`Detector.warm()` at start* on a real setup take. The first inference
    pays about 1 s of one-off costs, which otherwise landed on the first
    phrase after every boot.
- **Noise floor = 15th percentile of the last 2 s of frame RMS**, ignoring
  exact zeros. The old creeping floor sank to nothing on digital silence
  (TTS padding, and also mics with suppression or a gate). Room hiss then
  counted as speech for about two minutes, so the stop phrase's burst never
  ended.
- **Voice commands** (`commands.py`, `desktop.py`). Each command is a few
  spoken patterns (`[optional]`, `(a|b)`, a `{slot}` at the end), matched
  with wake's worst-word alignment at 0.8. The whole utterance must be the
  command, give or take a lead-in ("okay", "please"). Actions run on one
  worker thread (`Runner`), because UI Automation is COM.
  - After the stop phrase a window opens (`command_window`, 5 s), and
    reopens for 5 s when the text is pasted (`_delivered_now`). The live log
    showed every missed "submit" came after a long dictation, where the
    paste landed 4-6 s after the stop phrase. A burst in the window that
    parses runs it. A slot command must resolve ("go to the shops" is talk).
    One heard before the paste waits for `finish_external(done=...)`. Key
    commands run inside the target window, others after the paste.
  - "submit" waits 0.15 s before Enter, because Ctrl+V is processed
    asynchronously and an earlier Enter overtook it.
  - **Command mode**: the start phrase plus "command mode", or plus a
    resolvable command (in the same burst, or the first burst within 4 s of
    the start, which then cancels the fed dictation). Every burst is then a
    command until "exit", the stop phrase or `command_mode_seconds`. The
    start phrase there starts a dictation. `State.COMMAND` drives a teal pill.
  - The **LLM layer** (`plan_with_llm`) only runs in command mode, for
    utterances the patterns miss, with the cleanup model. JSON output, steps
    validated against `BY_NAME`; unknown ones are dropped. Plans with a
    `risky` command (close, submit) wait for "yes".
  - `desktop.py`: windows via EnumWindows (visible, unowned, not cloaked),
    named by exe stem, FileDescription and aliases, then title. Apps from
    `shell:AppsFolder` (desktop and Store apps), launched through
    `os.startfile`, never a console. Buttons via UI Automation
    (`FindAllBuildCache` on enabled, on-screen controls), invoked through
    Invoke/SelectionItem/Toggle/ExpandCollapse, or a click at the centre.
- **Near misses**: a phrase scoring 0.7 to the accept level is logged with
  just the matching words, and `Listener.last_near` feeds the tray's
  "Accept ... as the start phrase".
- **Paste where I started** (`inject.paste_to = "start"`, `window.py`). The
  foreground window and its focused control are captured when a dictation
  starts (`Engine._capture_target`). `Engine.in_target` brings that window
  forward, pastes, runs the `done` callback (so a pending "submit" presses
  Enter there), then re-activates the window that had focus.
  - Windows only lets the process that sent the last input change the
    foreground, so `window.activate` taps `VK_MASK` (marked `OWN_INPUT`)
    first, falls back to `AttachThreadInput`, and verifies the switch.
  - If the window is gone, or won't come forward, the text goes to the
    clipboard and the state is an error. Never type into a guessed window.
  - A "submit" said after the paste goes back to `Engine.last_target` for
    the Enter.
  - Tested across processes (the foreground lock doesn't apply within one).
  - Window titles are kept out of the log: they can be personal.
- `tests/always_on.py` checks the whole loop with TTS: enrol, a conversation,
  no pause after the start phrase, and four near-miss sentences. Measured on
  this PC: about 1% of one core and about 160 MB while listening.

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
uv run python tests/always_on.py            # always-on end to end with TTS (CPU)
uv run python tests/paste_target.py         # paste where I started (shows 2 windows)
uv run python tests/desktop_commands.py     # voice commands on a test window (+ LLM layer)
uv run localflow listen                     # always-on in the console, pastes nothing
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

## One tray app, one icon

A work PC collected a live tray icon per restart (three copies running, one an
older build). `restart.ps1` finds the old copy by command line. A run without
admin rights can't read an elevated process's command line, so it found
"nothing" and started a second copy. Now there are three layers:

1. **Clean handoff.** Restart writes `.quit`; the app (`cli._watch_quit_file`)
   quits through the tray, which removes the icon properly. A killed process
   leaves its icon until the mouse passes over it. A file works across
   elevation; signals don't. Hard kill only after 8 s. The installer and
   updater stop it the same way.
2. **Privilege.** A blank command line *and* path counts as ours. A
   non-elevated `restart.ps1` hands the restart to the elevated `LocalFlow
   resume` task up front, rather than starting a non-admin copy.
3. **Single instance.** `cli._claim_single_instance`: a named mutex
   (`Local\LocalFlowTrayApp`). A second copy waits 8 s for the first to go,
   then exits 0, so the supervisor doesn't relaunch it. Access denied (the
   owner is elevated) also counts as "exists".

Tested on this PC: elevated restart, a restart from a non-admin scheduled task,
and a deliberate second launch. Each left exactly one copy.

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
