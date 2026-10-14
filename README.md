# LocalFlow

Free, private dictation for Windows. Tap **Caps Lock**, speak, tap it again, and
your words appear in whatever app you're typing in, cleaned up and punctuated,
with the "um"s and false starts removed.

Everything runs on your own PC. Whisper turns speech into text; an optional
local LLM (via [Ollama](https://ollama.com)) tidies it up, or a built-in
rule-based cleaner does it with no LLM at all. No audio or text ever leaves
your machine, there is no account, and nothing costs money.

- **Works in any app**: the text is pasted into the focused window.
- **Live feedback**: a small pill above the taskbar shows a waveform, the words
  as you say them, and a check mark when the text is in.
- **Smart cleanup**: "send it Tuesday, no wait, Wednesday" becomes "Send it
  Wednesday." Spoken "comma", "new paragraph" and so on are applied.
- **No GPU or LLM required**: it falls back to CPU transcription and
  algorithmic cleanup, so it runs on an ordinary laptop.
- **Extras**: rewrite in a character or emotional tone, pick sound cues, and
  dictate from your phone.

Measured on an RTX 4070 Ti with `large-v3-turbo`: 21 s of speech → 1.7 s
transcription + 0.6 s LLM cleanup, about 12× realtime.

---

## Install

You need **Windows 10 or 11** and a microphone. Everything else is installed
for you.

```powershell
git clone https://github.com/taylormartins/LocalFlow.git
cd LocalFlow
.\install.cmd
```

No git? Click **Code ▸ Download ZIP** on GitHub, unzip it anywhere (say
`Documents\LocalFlow`), and double-click **`install.cmd`**.

The installer is safe to re-run at any time. It:

1. installs [uv](https://docs.astral.sh/uv/) (a Python package manager) if you
   don't have it, then Python 3.12 and all dependencies into a private `.venv`
   folder (about 1.5 GB to download; no admin needed);
2. checks for an NVIDIA GPU. Without one, it uses a smaller Whisper model on
   the CPU;
3. checks for Ollama and the cleanup model. Without them, cleanup is the
   built-in algorithmic one;
4. creates your `config.toml` and downloads the Whisper model once (about
   1.6 GB with a GPU, 0.5 GB without);
5. sets LocalFlow to start when you log in and after sleep. **This asks for
   admin once** (a UAC prompt). Without admin rights it uses an ordinary
   Startup shortcut instead, and everything works except dictating into apps
   that themselves run as admin;
6. starts LocalFlow. Look for the microphone icon in the system tray (it may
   be hidden under the `^` arrow next to the clock).

| Installer option | Effect |
|---|---|
| `-Cleanup rules` | Force the algorithmic cleanup (no LLM), even if Ollama is installed. |
| `-Cleanup llm` | Force LLM cleanup. |
| `-PullModel` | If Ollama is installed but the cleanup model isn't, download it (4.7 GB). |
| `-NoStartup` | Don't register it to start at login (no UAC prompt). |
| `-NoLaunch` | Don't start it at the end. |
| `-WhisperModel <name>` | Speech model: `tiny.en` (78 MB), `base.en` (148 MB), `small.en` (486 MB, default without a GPU), `large-v3-turbo` (default with one). Smaller is faster and less accurate. |
| `-ModelZip <zip>` | Offline install: use a model zip made on another PC (see below). |
| `-ModelDir <folder>` | Offline install: use a model folder you downloaded yourself. |
| `-HfEndpoint <url>` | Download models from an internal mirror instead of huggingface.co. |
| `-Uninstall` | Stop LocalFlow and remove the startup entries. |

Pass them to either script: `.\install.cmd -Cleanup rules`, or
`powershell -ExecutionPolicy Bypass -File install.ps1 -Cleanup rules`.

### Optional: LLM cleanup with Ollama

The algorithmic cleanup is good. An LLM is better at untangling rambling
sentences. With an NVIDIA GPU (8 GB+ VRAM recommended):

1. Install [Ollama](https://ollama.com/download).
2. Run `.\install.cmd -PullModel -Cleanup llm`.

You can switch between the two at any time from the tray: right-click ▸
**Cleanup model** ▸ **Algorithmic** or a model name.

### Locked-down networks (corporate proxy, no huggingface.co)

Everything LocalFlow downloads comes at install time. Python packages come from
PyPI. The speech model comes from Hugging Face: about 0.5 GB on CPU, 1.6 GB on
GPU. After that it runs with no network at all. Work networks get in the way in
three ways:

- **The proxy inspects HTTPS (Zscaler, Netskope and the like).** The installer
  and the model download both retry using the Windows certificate store, where
  IT installs the proxy's certificate. Nothing to do.
- **The network blocks huggingface.co outright.** Nothing to do either.
  LocalFlow keeps its own copies of the small CPU models (`tiny.en`,
  `base.en`, `small.en`) on GitHub, which a network that let you `git clone`
  almost always allows. It tries a release download first, then plain git over
  github.com, and checks every file's SHA-256 against the value pinned in the
  code. On a GPU PC the large model isn't mirrored, so it falls back to
  `small.en` (on the GPU it's still quick). For the smallest download, install
  with `-WhisperModel base.en` or `-WhisperModel tiny.en`.

  If GitHub is blocked too, the install still finishes and tells you why (for
  example *blocked by Zscaler, HTTP 403*). Then bring the model over by hand.
  Each one is a zip on the
  [models release](https://github.com/taylormartins/LocalFlow/releases/tag/models-v1),
  or you can make one on any PC with internet:

  ```powershell
  uv run localflow fetch-model --model small.en --zip D:\
  # -> D:\localflow-model-small.en.zip. Copy it over, then on the work PC:
  .\install.cmd -ModelZip D:\localflow-model-small.en.zip
  ```

  A model folder downloaded by hand from
  [Systran's models](https://huggingface.co/Systran/faster-whisper-small.en/tree/main)
  (`config.json`, `model.bin`, `tokenizer.json`, `vocabulary.txt`) works too:
  `.\install.cmd -ModelDir <folder>`. Sideloaded models live in `models\`
  inside the LocalFlow folder.
- **Your company runs an internal mirror (Artifactory, Nexus).** Use
  `.\install.cmd -HfEndpoint https://your-mirror/...`. That saves it as
  `whisper.hf_endpoint` in `config.toml`.

`uv run localflow check` reports which of these you're hitting.

### Updating

```powershell
cd LocalFlow
git pull
.\install.cmd
```

Your `config.toml` is never overwritten. If the update fails with files in use,
quit LocalFlow from the tray first.

### Uninstalling

Run `.\install.cmd -Uninstall`, then delete the folder. Whisper models stay
cached in `%USERPROFILE%\.cache\huggingface`; delete that too to reclaim the
space.

### For AI agents installing this

Do the install non-interactively, then verify it:

```powershell
git clone https://github.com/taylormartins/LocalFlow.git
cd LocalFlow
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -NoStartup
uv run localflow check                       # GPU / mic / cleanup / hotkeys report
uv run localflow clean "um so send it tuesday no wait wednesday"
# expected: "Send it Wednesday."
```

- If Hugging Face is blocked, the installer gets the model from LocalFlow's
  own GitHub copies on its own (watch for `installed small.en at ...\models`).
  Only if that fails too does `== Whisper model` end with *NOT READY YET*. The
  rest of the install is still done then. Do not try to route around the
  block with third-party mirrors. Hand the user the manual steps under
  *Locked-down networks* above (a zip from the models release, then
  `-ModelZip`). If
  `uv sync` itself fails on certificates, the installer already retries with
  `UV_SYSTEM_CERTS=1`.
- `-NoStartup` avoids the UAC prompt, which an agent can't click. Afterwards,
  ask the user to run `.\install.cmd` once themselves so it starts at login.
- The installer exits non-zero with a message on any failure; every step
  prints `== <step>` first.
- Logs are in `localflow.log` in the repo folder. Whether the app is running:
  `Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | ? CommandLine -match localflow`.
- Do not commit `config.toml` (the user's settings) or `data/` (a lexicon whose
  licence forbids redistribution); both are gitignored.
- Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing code. It lists the
  traps that each cost real debugging time.

---

## Using it

| Key | Action |
|---|---|
| **Caps Lock** or **Ctrl+Win** (or F9) | Start / stop dictation, cleaned up |
| **Shift+Caps Lock** or **Ctrl+Win+Shift** (or F10) | Same, but verbatim: no cleanup or rewriting |
| **Esc** | Cancel the current recording without transcribing |

Use the raw keys when you need exact text: code, quotes, anywhere a helpful
rewrite would be wrong.

**Choosing keys.** Right-click the tray icon ▸ **Dictation keys**, and tick any
mix of the following. Changes apply immediately, with no restart:

| Key | Raw (verbatim) version |
|---|---|
| Caps Lock | Shift+Caps Lock |
| Ctrl+Win | Ctrl+Win+Shift |
| F9 | F10 |
| Scroll Lock | Shift+Scroll Lock |
| Pause/Break | Shift+Pause/Break |
| None | No dictation key at all |

While Caps Lock is a dictation key, it no longer toggles capitals. If capitals
are stuck on (say Caps Lock was on before LocalFlow started), LocalFlow turns
them off at startup. A **Turn Caps Lock off** item also appears in the tray
menu whenever capitals are on. Untick Caps Lock to get the normal key back.

Tap-to-toggle is the default so you can start a recording and keep working. For
push-to-talk instead, set `hotkeys.mode = "hold"` in `config.toml` and the same
keys record only while held. Other key combinations can be set by hand under
`[hotkeys]`. The tray menu lists them and leaves them alone.

### Always-on mode (hands-free)

Say a **start phrase** to begin dictating and a **stop phrase** to finish, with
no keys at all. For example, say *"Okay listen, Marigold"*, dictate, then say
*"That will be all"*. The text is pasted just as if you'd used a hotkey, minus
both phrases.

**Setting it up.** Right-click the tray icon ▸ **Set up always-on
phrases...**:

1. Type a start phrase and a stop phrase. Pick ones you'd never say by
   accident: three or four words, ideally with a name in them.
2. Under **Other spellings**, add other ways a word could be written
   (`Marygold, Merrigold`). A single word replaces the closest word in your phrase,
   so it never becomes a trigger on its own.
3. Record each phrase **3 times**. Press **Space** (or click **Record**). The
   button turns amber for a moment, then **green**: speak, then press Space
   again. The pause before green, and a trim at the end, keep the keyboard or
   mouse click out of the take.
4. Under each take you'll see what LocalFlow heard. Those spellings are learned
   too, because the listener will hear your voice the same way next time.
   Click a take to record it again.
5. **Save**. Listening starts right away. Turn it on or off with tray ▸
   **Always-on listening**.

While dictating hands-free, a hotkey tap or Esc also ends it. So does 45
seconds of silence (`always_on.idle_stop_seconds`), so a forgotten stop phrase
can't record the room indefinitely.

**How it decides, and what it costs.** The mic stays open. Between phrases, the
only work is a loudness check on each 30 ms of sound, and audio lives in a
30-second buffer that is constantly overwritten. Nothing is saved or logged.
Only bursts of speech are passed to a small speech model (`base.en`, 2 CPU
threads, about 160 MB of RAM). A phrase counts when what it hears matches one
of the accepted spellings closely, word for word. A similar word doesn't
count: with "Marigold" in the phrase, "okay listen, Mary told me" is refused.
It must also sound like your recorded takes. In testing, near misses like
that, and the stop phrase said in passing, did not trigger it. If your real
phrase is sometimes missed, run `localflow listen` to see how it was heard,
and add that spelling in setup. Measured: about 1% of one CPU core
while listening.

Your phrases and voice takes are stored in `wake\` inside the LocalFlow folder,
on your PC only. To try phrases out without pasting anything, run
`uv run localflow listen`. It prints what it hears and when the phrases are
caught.

While recording, a small pill appears just above the taskbar. Its bars scroll
with your voice, bright while you speak and dim in silence, beside a timer. A
spinner shows while it transcribes and cleans up, and a check mark appears when
the text is inserted.
The pill's dot is **green** when the LLM cleanup will run, **blue** for
algorithmic cleanup (chosen in the tray, or Ollama/the model unreachable), and
**white** for raw text: raw mode, or cleanup switched off in the tray. If the LLM
was expected but failed, the algorithmic cleanup steps in and the check turns
amber: "Inserted - LLM off, basic cleanup". The tray icon also turns red. Turn the pill off with
`overlay.enabled = false`.

**Live text.** While you talk, the sentence you're speaking appears under the
waveform, updated about once a second. When you move on to a new sentence, the
last few words of the previous one stay visible, dimmed. The text shrinks to fit
two lines, and very long sentences show "…" plus the newest words. It's display
only: the pasted text still comes from the full transcription after you stop.
Tune it under `[overlay]`: `live_text`, `live_interval`, `live_context_words`,
`live_max_words`, `live_window`. It pauses itself when Whisper runs on the CPU.

**Character and tone.** Needs Ollama. Right-click the tray icon and use **Character ▸** and
**Tone ▸**. They stack: Rogue + Rage is one rewrite. Characters: humanize,
professional, tech professional, doctor, rogue, robot, caring mother, defiant
worker, plus your own under `[style.characters]`, e.g.
`pirate = "a salty pirate captain; nautical slang, gruff but cheerful"`. Tones
follow Plutchik's wheel of emotions: joy, trust, fear, surprise, sadness, disgust,
anger and anticipation, each at mild / moderate / intense (annoyance → anger →
rage). The pill shows "Styling: …" while it works. Raw mode (Shift+Caps) never
styles. If the rewrite fails the safety checks (answered your dictation instead
of rewriting it, added profanity or threats, dropped what you said, or grew far
too long), you get the plain cleaned text and the pill says "Inserted - style
skipped". **Style model ▸** in the tray picks the model for this. With "Same as
cleanup" (qwen2.5:7b), cleanup runs and then styling runs, adding about 0.35 s.
A bigger model such as **gemma4:12B** gives much stronger voices. When the style
model differs from the cleanup model, it does cleanup *and* style in a single
pass (about 1.3 s warm), because two models don't fit a 12 GB card together:
Ollama swapping them cost about 55 s per dictation. Expect a one-time ~30-50 s
load the first time after switching.
Test changes with `uv run python tests/style_quality.py`.

Extra tone vocabulary comes from the **NRC Emotion Intensity Lexicon** (Saif M.
Mohammad, National Research Council Canada), which is free for non-commercial
research and educational use and may not be redistributed. It is therefore
*not* in the repo: `uv run python tools/build_tone_vocab.py` downloads it into
`data/` (gitignored) and builds `data/tone_vocab.json`. Without it, tones use
only the built-in word lists in `style.py`.

**Cleanup model.** Right-click the tray icon, then **Cleanup model: … ▸**.
**Algorithmic (no LLM, instant)** needs nothing. Below it are your installed
Ollama models. LocalFlow unloads the old one and loads the new one in the
background, and the choice is saved to `config.toml`.
Use **Refresh list** after pulling a new model. `qwen2.5:7b` is marked *tested*:
it's the model `tests/cleanup_quality.py` was tuned against. Other models work
but may answer dictated questions instead of transcribing them, so run that suite
before settling on one. Models over ~8.5 GB are marked *too big for GPU*; they
spill into system RAM, and cleanup slows from about a second to tens of seconds.

**Sounds.** Three cues play: one when recording starts, one when it stops, and a
barely-there "faint pop" once the text has actually been pasted. Esc cancels
silently, and a failed paste plays no paste sound. Right-click the tray icon and
use **Start sound ▸**, **Stop sound ▸** and **Paste sound ▸** to choose from tick,
pop, faint pop, blip, click, chime up, chime down, soft or none. Picking one plays
it and saves it to `config.toml`. Under `[sounds]` you can also give a path to your
own `.wav`. The start cue is silenced out of the recording, because Whisper can
turn a lone "pop" into made-up text.

**Why it runs as admin.** The installer registers two scheduled tasks that start
it with highest privileges: **LocalFlow** at logon and **LocalFlow resume** on
wake from sleep. Windows won't send keystrokes to a normal program's keyboard
hook while an admin window (an elevated terminal, say) has focus, so a non-admin
LocalFlow would ignore your hotkey there. Restarting on wake matters because
sleep can silently drop the keyboard hook.

**Crashes restart themselves.** `restart.ps1` stays running as a hidden
supervisor and relaunches the app after a native crash (exit `0xC0000409`). It
gives up after 3 crashes in 10 minutes. Python stacks from a crash go to
`crash.log`; the restarts are logged in `localflow.log`.

**If the hotkey stops responding, or text stops getting cleaned up:** run
`schtasks /run /tn "LocalFlow resume"` (or that task in Task Scheduler).
`restart.ps1` waits for Ollama's models folder, starts Ollama if it is down, and
relaunches the tray app. Each run writes `localflow.restart` lines to
`localflow.log`. Double-clicking `restart.vbs` also works, but runs non-elevated
and can't stop an admin copy; it will say so in the log.

If you move the LocalFlow folder, run `.\install.cmd` again so the startup
entries point at the new location.

---

## Commands

```powershell
uv run localflow run                  # tray + hotkeys (+ phone server if enabled)
uv run localflow check                # diagnose GPU / model / mic / Ollama / hotkeys
uv run localflow fetch-model          # download the speech model (--zip DIR: for another PC)
uv run localflow import-model x.zip   # install a model from a zip or folder (offline)
uv run localflow devices              # list audio inputs
uv run localflow level                # live mic meter - run this first if nothing works
uv run localflow record -s 5          # record 5s from the mic and transcribe
uv run localflow transcribe clip.wav  # transcribe a file (--rules: no LLM)
uv run localflow clean "some text"    # run the cleanup on typed text (--llm: Ollama)
uv run localflow serve                # phone server only, no hotkeys
uv run localflow install-startup      # run at login (--remove to undo)
uv run localflow enroll               # set up always-on start/stop phrases
uv run localflow listen               # try always-on in the console (--file clip.wav)
```

Working on the code? Read **[ARCHITECTURE.md](ARCHITECTURE.md)** — it covers the
module layout, the design decisions, and the non-obvious traps that each cost
real debugging time. Integration checks live in `tests/`:

```powershell
uv run python tests/cleanup_quality.py   # gate any prompt or model change on this
uv run python tests/rules_quality.py     # algorithmic cleanup; no GPU or Ollama needed
uv run python tests/always_on.py         # always-on mode end to end (Windows TTS voice)
uv run python tests/bench_models.py      # latency vs quality across models
uv run python tests/hotkeys.py           # hold / toggle / cancel state machine
```

`record` is the fastest way to check your mic actually works — it prints the peak
signal level, so a silent mic shows up immediately.

---

## How it works

```
mic ──► Whisper ──► cleanup (Ollama or rules) ──► clipboard ──► Ctrl+V into focused app
                                    │
phone (PWA) ──► HTTP ───────────────┘──► text back to phone
```

Both front-ends share one engine (`engine.py`), so the phone gets exactly the
same transcription quality as the desktop.

**Text injection** works by putting the result on the clipboard and sending
Ctrl+V, then restoring the previous clipboard contents. It is far faster than
simulating keystrokes and handles Unicode correctly. If an app refuses paste, set
`inject.method = "type"` in `config.toml`.

**The cleanup pass** is the interesting part. A raw transcript of
*"So um, I want to add a dash attack. Actually no, make it a dodge roll instead"*
becomes *"I want to add a dodge roll to the player character."* — it resolves
self-corrections, not just filler.

The real risk with an LLM in this position is that it *answers* your dictation
instead of transcribing it — say "can you hear me?" and get back "Yes, I can hear
you." A system prompt alone does not stop a 7B model doing this. Four things do:

1. **Few-shot examples** (`FEW_SHOT` in `cleanup.py`) demonstrating questions
   that stay questions. This is what actually carries the behaviour; the prompt
   only supports it.
2. **Temperature 0**, so the behaviour is reproducible.
3. **An opener check** that rejects output starting with an acknowledgement the
   speaker never said ("Yes,", "Sure,", "Of course"). It compares against the raw
   text with leading filler stripped, so a legitimate "so I think…" → "I think…"
   is not mistaken for one.
4. **A length-ratio and code-block check**, catching the case where it answers at
   length or emits a function you only asked it to describe.

Any rejection falls back to the algorithmic cleanup, as does a cleanup
timeout or an unreachable Ollama. You never lose your words to a failed cleanup.

Verified against 14 adversarial inputs, and the five worst offenders repeated
three times each for stability. One cosmetic miss remains: very short imperatives
("tell me a joke") sometimes get a question mark.

If you hit a case where it answers you, add the input and the correct output to
`FEW_SHOT` — that list is the intended place to teach it.

**Algorithmic cleanup** (`rules.py`) is the no-LLM alternative: about 2 ms, no
Ollama, no GPU. Because it never writes words of its own, it can't answer you.
It only deletes, punctuates and recases:

- filler: um, uh, er, hmm always; "you know", "I mean", "like" only where they
  are plainly filler (", you know,", "at like sixty")
- stutters: "the the", "I think, I think", "w- we"
- self-corrections with a spoken marker ("no wait", "actually no", ", I mean",
  "or rather", "scratch that"): "send it Tuesday no wait Wednesday" →
  "Send it Wednesday."
- spoken commands: "comma", "period", "question mark", "new line", "new
  paragraph", "open quote" / "close quote"
- sentence case, "i" → "I", "dont" → "don't", days and months, your
  `vocabulary.terms` spelled exactly, and a closing "." or "?"

Where a rule would have to guess, it leaves the words alone. What it can't do is
rephrase: a correction with no marker, or a sentence that only reads well once
reworded, needs the LLM. Try it on typed text with
`uv run localflow clean "um so send it tuesday no wait wednesday"`, and gate any
change on `tests/rules_quality.py`.

---

## Performance

Cleanup latency, warm, with nothing else competing for the GPU:

| Dictation | LLM called? | Time |
|---|---|---|
| Short and already clean | no | **0ms** |
| Short with filler | yes | ~235ms |
| Medium with filler | yes | ~380ms |
| Long, rambling, self-correcting | yes | ~610ms |

Three things make that work:

- **The LLM is skipped entirely** when the transcript has no filler, stutter,
  spoken punctuation, or self-correction (`skip_if_clean`). Crisp sentences cost
  nothing at all.
- **The few-shot prefix is cached by Ollama.** Those 670 prompt tokens re-evaluate
  in ~12ms, so the examples that keep it honest are nearly free. Do not trim them
  for speed — they are not what costs you.
- **Output length drives the rest.** Generation runs at ~90 tok/s, and the model
  rewrites your whole sentence, so time scales with how much you said.

**If cleanup feels slow, it is almost always VRAM contention, not the model.**
Measured on a 12 GB card: with a game engine editor open and three Ollama models
pinned, the same call took **12–20 seconds** instead of 0.4s. Diagnose with:

```powershell
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
ollama ps                  # anything you are not using?
ollama stop <model>        # evict it
```

Benchmarking several models leaves them all resident for `keep_alive`, which is
the easiest way to do this to yourself.

---

## Configuration

Everything lives in `config.toml`. The interesting knobs:

| Setting | Notes |
|---|---|
| `audio.device` | Substring of the input name, e.g. `"Scarlett Solo"`. `""` = the Windows default mic. `uv run localflow devices` lists them. |
| `audio.boost` | Mic boost: `none`, `low`, `medium`, `high` or `auto` (also in the tray). |
| `audio.gain` | Extra fixed multiplier before the boost; rarely needed. |
| `whisper.model` | `large-v3-turbo` is the sweet spot on a GPU. `large-v3` is more accurate but slower; `distil-large-v3` is faster, English only. On CPU: `small.en` or `base.en`. |
| `whisper.device` | `cuda` (NVIDIA GPU) or `cpu` (with `compute_type = "int8"`). |
| `cleanup.method` | `llm` (default; algorithmic fallback when Ollama is unavailable) or `rules` (algorithmic only, no LLM). Also in the tray. |
| `cleanup.model` | `qwen2.5:7b` (default) ~607ms on a long dictation. `qwen2.5:3b` is 436ms but stops resolving self-corrections. |
| `cleanup.skip_if_clean` | Bypass the LLM when there is visibly nothing to fix. Leave on. |
| `cleanup.instructions` | Your house style, appended to the prompt. Keep it short. |
| `hotkeys.mode` | `hold` (push-to-talk) or `toggle` (tap on, tap off). |
| `vocabulary.terms` | Proper nouns Whisper mangles. Fed to Whisper as a decoding hint *and* to the cleanup model as a spelling authority. |
| `vocabulary.replacements` | Literal find/replace applied last. |

The vocabulary list is worth maintaining — it is the difference between
"post gress" and "PostgreSQL".

---

## iPhone

The PWA is built in and served by the desktop app once you turn the server on:
set a token and `server.enabled = true` under `[server]` in `config.toml`, then
restart LocalFlow. One constraint drives the whole setup: **iOS Safari only grants microphone access over HTTPS.** A bare
`http://192.168.x.x:8123` will load the page but the mic button stays disabled.

Tailscale solves this with a real certificate and no port forwarding.

### 1. Install Tailscale on the PC and the iPhone

Download from <https://tailscale.com/download>, sign in with the same account on
both. Free for personal use.

### 2. Expose LocalFlow over HTTPS

With `localflow run` already going, in a terminal on the PC:

```powershell
tailscale serve --bg 8123
tailscale serve status    # prints your https://<machine>.<tailnet>.ts.net URL
```

That URL is HTTPS, valid only inside your tailnet, and reachable from anywhere —
coffee shop, phone on cellular, wherever.

### 3. Add it to the home screen

On the iPhone, open that URL in **Safari** (not Chrome — only Safari can install
PWAs on iOS), then **Share → Add to Home Screen**. Launch it from the icon so it
runs fullscreen without browser chrome.

### 4. Paste in the token

Tap the ⚙ icon and enter the `server.token` value from `config.toml`. Then hold
the big button, speak, release, and tap **Copy**.

> **What the iPhone cannot do:** insert text directly into other apps. iOS only
> allows that from a native keyboard extension, which needs a Mac, Xcode, and an
> Apple Developer account. Until then the flow is dictate → Copy → paste. If you
> later want the real thing, the server API is already the right shape for a
> native client to talk to.

### Security

The token is checked with a constant-time comparison, and Tailscale means the
port is never exposed to the public internet. Leaving `server.token` empty
disables auth entirely — only reasonable if you also set
`server.host = "127.0.0.1"`.

---

## Run at login

Already installed. To manage it:

```powershell
uv run localflow install-startup            # install (idempotent)
uv run localflow install-startup --remove   # uninstall
```

That writes `LocalFlow.lnk` into your Startup folder, pointing at the venv's
`pythonw.exe` — no console window, and it does not depend on `uv` being on PATH
at login. To start it right now without logging out, double-click
`localflow.vbs`.

Because there is no console when it starts at login, `localflow run` always
writes a rotating **`localflow.log`** in the project root. Check there first if
the tray icon does not appear.

> Windows 11 hides new tray icons. Click the **`^` chevron** left of the clock to
> find it, and drag it onto the taskbar to pin it.

---

## Troubleshooting

**Transcripts come back empty.**
Run the level meter and speak:

```powershell
uv run localflow level
```

It prints a live dB bar and a verdict, then tells you what to do. `silent` means
no signal at all — check the gain knob, the 48V switch if your mic needs phantom
power, the cable, and Windows mic privacy settings. `quiet` means speech is
present but Whisper's VAD will discard some of it. The meter tells you which
**Mic boost** to pick.

**Mic is quiet even when you're close to it.** Right-click the tray icon ▸
**Mic boost**:

| Setting | Boost | When |
|---|---|---|
| None | 0 dB | Mic is already at a healthy level (an audio interface with its gain set) |
| Low / Medium / High | +6 / +12 / +18 dB | A mic that's consistently quiet by a known amount |
| Auto | up to +26 dB | Not sure, or you switch mics. It measures each recording and lifts the speech. |

Auto never turns anything down, leaves recordings that are already loud
alone, and won't boost an empty room. If a recording has no clear speech above
the background, it isn't touched. It never lifts the background above about
−40 dB, so Whisper isn't handed amplified hiss to invent words from. Loud
peaks after a boost go through a soft limiter rather than hard clipping. New
installs start on Auto. Fix the mic's own gain first if it has a knob: a
software boost raises the noise along with the voice.

Two non-obvious causes, both seen in practice:

- **Windows capture volume.** Separate from an interface's physical gain knob and
  it can reset on reboot. Sound settings → Input → your mic → Volume. At 50% it
  halves everything.
- **A wedged host API.** MME in particular will open the device happily and then
  deliver no audio at all. The recorder now detects a capture with zero blocks,
  drops that host API for the session, and moves down the fallback chain. The log
  line to look for is `delivered no audio at all`.

Every capture now logs its level, so `peak` and `rms` in the log tell you
immediately whether the microphone or the pipeline is at fault.

**First dictation after boot is slow.**
Expected. Whisper loads weights (~7-30 s) and Ollama pulls the cleanup model
into VRAM. The tray app warms both at startup, so this usually happens before
you need it.

**Everything is slow, not just the first run.**
Something else is using the GPU. Game engines and 3D apps in particular will
roughly halve throughput. `nvidia-smi` and `ollama ps` will show you what is
resident. On a CPU-only machine, use `whisper.model = "base.en"` for speed.

**Cleanup times out.**
VRAM pressure — Whisper, the cleanup model, and whatever else you have open do
not all fit. Either close the other GPU app, drop to a smaller cleanup model, or
pick **Algorithmic** in the tray. A failed LLM call falls back to the algorithmic
cleanup either way.

**The hotkey also triggers something in the focused app.**
It should not — the Win32 event filter suppresses the bound keys specifically.
If a particular app still sees it, that app is reading the raw input device;
rebind to something unusual like `<ctrl>+<alt>+d`.

**Hotkeys do nothing in an elevated window.**
Windows blocks input from a normal process into an elevated one. Re-run
`.\install.cmd` and accept the admin prompt so LocalFlow starts elevated.

---

## License and credits

MIT © Taylor Martins. See [LICENSE](LICENSE).

Built on [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (OpenAI
Whisper via CTranslate2), [Ollama](https://ollama.com),
[pynput](https://github.com/moses-palmer/pynput) and
[pystray](https://github.com/moses-palmer/pystray). The optional tone
vocabulary uses the NRC Emotion Intensity Lexicon, downloaded on demand and
never redistributed (see above).
