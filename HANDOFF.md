# Handoff: Golden Flow (the `localflow` package)

For the next developer or agent. Read this first, then
[ARCHITECTURE.md](ARCHITECTURE.md) for how each part works and the traps,
[README.md](README.md) for what users see, and
[docs/CAMERA_MODE.md](docs/CAMERA_MODE.md) for the camera plan.

Written 2026-10-18 at v1.27.0; last updated 2026-10-24 at v1.34.1, when the
original developer (a Claude session) handed the project on. Everything
below is current as of that release.

## Start here (your first hour)

1. **Set up**: install [uv](https://docs.astral.sh/uv/), then in the repo
   `uv sync --inexact` (add `--extra camera` for camera mode). Python 3.12
   is fetched by uv. On a Mac, `bash install.sh --no-start` does the same
   plus the Whisper model.
2. **Prove the tree is green**: run the no-window suites (seconds; no GPU,
   no models, no windows):
   ```
   for t in learning quick_mode edit_commands live_stop command_phrases \
            enroll_redraw ollama_health layouts camera_mode settings_apply; do
     uv run python tests/$t.py | tail -1; done
   ```
   Each prints `all passed`. CI runs the same on macOS and Windows.
3. **Read** this file, then ARCHITECTURE.md ▸ "Five things that will bite
   you", then the module you're changing. The big modules have a docstring
   at the top saying what they own and why.
4. **Look at a real log**: the user's `localflow.log` (gitignored) shows every
   start/stop phrase, command, paste and timing. Most bug reports are
   explained there before you read any code.
5. **Pick work** from "What's next" at the bottom. Mac stage 2 is the most
   wanted: the user has a MacBook ready for it.


---

## What this is

A Windows and macOS tray / menu-bar app that turns voice (and, starting now, a webcam) into full
control of the PC, with no typing:

- **Dictation** into any app: Whisper on the GPU (CPU fallback), cleanup by a
  local LLM through Ollama or by rules alone, pasted where you started.
- **Always-on listening**: a start phrase and a stop phrase (set up and
  voice-checked per user), with commands right after the stop phrase
  ("submit", "cancel").
- **Command mode**: windows, tabs, keys, numbered click labels, a mouse grid,
  window arrangement and saved layouts, edit by voice, a command sheet,
  learning new phrasings, an optional LLM layer for requests the patterns
  don't cover (it can only pick from the same command list).
- **Quick mode** and the **do word** for speed ("<start> do enter").
- **Themes** for the pill, the labels, the command sheet and a paste effect.
- **Camera mode, part one**: which screen you're looking at.

It must work with algorithms alone. The LLM is always optional, and every LLM
path has a rules or pattern fallback.

## Names

- **Golden Flow** is the name users see (`localflow.APP_NAME`; it was
  LocalFlow until v1.27). Change it there and the tray, notifications,
  window titles and Settings follow.
- Everything else is still `localflow`: the Python package, the repo
  (github.com/taylormartins/LocalFlow), the folder, the `localflow` command,
  the scheduled tasks ("LocalFlow" at logon, "LocalFlow resume" on wake),
  the single-instance mutex, the phone API's header, and `APP_ID`
  ("LocalFlow.App", Windows' notification identity). Renaming those would
  break existing installs and the updater; do it only as a planned migration.

## Layout

```
src/localflow/      the app (one package; see the module table below)
tests/              scripts that print pass/FAIL (see Testing)
docs/CAMERA_MODE.md the camera plan and its status
tools/              one-off data builders (tone vocabulary)
install.ps1 / update.ps1 / restart.ps1   ASCII only (PowerShell 5.1 reads them as ANSI)
config.toml         the user's settings (gitignored; config.example.toml is the template)
data/               generated and private files (gitignored): app icon, face model,
                    camera calibration, tone lexicon
learned.json, layouts.json, themes.json, history.jsonl, wake/, models/   private, gitignored
```

| Module | What it owns |
|---|---|
| `cli.py` | Subcommands; `run` wires everything (engine, listener, tray, overlay, camera, health, hotkeys). |
| `engine.py` | The dictation pipeline and its state machine; paste and the paste effect. |
| `audio.py`, `micwatch.py` | Microphone capture, gain, default-mic following. |
| `transcribe.py`, `models.py`, `_cuda.py` | Whisper and getting models onto the machine. |
| `cleanup.py`, `rules.py`, `style.py` | LLM cleanup with guards; rule-based cleanup; character/tone rewrite. |
| `health.py` | Watches Ollama; the tray's restart. |
| `inject.py`, `window.py` | Paste into the focused window; remember and return to a window. |
| `hotkey.py` | Global hotkeys (Caps Lock tap, Ctrl+Win, F9). |
| `wake.py` | Start/stop phrases: storage, fuzzy matching, the voice check (DTW). |
| `always_on.py` | The listener: bursts, phrases, command mode, quick mode, the do word, input buffering, camera gating. The busiest file. |
| `commands.py` | Every voice command: patterns, actions, undo, the command sheet (`HELP_SHEET`), learned phrases, layouts, the LLM planner. |
| `desktop.py` | Win32 / UI Automation: keys, windows, monitors, labels, arranging, layouts, the caret. |
| `edit.py` | Edit by voice (rules first, LLM for the rest). |
| `overlay.py` | Every on-screen layer: the pill, labels, window list, command sheet, paste effect, camera pill. One Tk thread. |
| `themes.py` | Theme data and every drawing function the overlay and Settings share (legacy v2 shapes; pixel themes dispatch to `pixel.py`). |
| `pixel.py` | The pixel themes (opt-in tick since 1.33.2; default is legacy): Silkscreen text, the segmented-border body, four looks (block, knight, wizard, electric), sprites, LED-style meters. House style: `C:\Code\design`. |
| `settings.py` | The Settings window (its own process): Appearance, Theme builder, Dictation, Listening, Sounds, Camera. |
| `tray.py` | The tray icon and menu; applies Settings changes live (`LIVE_SETTINGS`). |
| `camera.py` | Camera mode: the watcher thread, gaze geometry, calibration. |
| `icon.py` | The crown emblem, window icons, the app identity (notifications say Golden Flow). |
| `enroll.py` | Phrase setup window. |
| `history.py` | Dictation history window. |
| `sounds.py` | Generated cue sounds (tick, pop, click...). |
| `server.py`, `pwa/` | Phone dictation over HTTPS. |
| `updates.py` | Version check against GitHub releases. |

## What's built (the full inventory)

**Dictation**
- Hold-to-talk and tap-to-toggle hotkeys (Caps Lock tap, Ctrl+Win, F9 on
  Windows; Right Option on a Mac), and a raw variant without cleanup.
- Whisper (large-v3-turbo on the GPU, CPU int8 fallback), models fetched
  once; a small `base.en` for listening, sharing the GPU.
- Cleanup: an Ollama LLM with guards (meaning kept, length checks, fallback
  on timeout) or rules alone (`cleanup.method = "rules"`); a style / tone
  rewrite (`style.py`); terminals get their lines joined.
- Paste where you started (the window is remembered at the start), the
  clipboard restored; a history window; phone dictation over HTTPS
  (`server.py`, `pwa/`); a live preview on the pill while you talk.

**Always-on listening** (`always_on.py`, `wake.py`)
- Per-user start and stop phrases, set up and voice-checked (DTW) in the
  phrase setup window; mishearing tolerance; second listens with the big
  model for near misses.
- After the stop phrase: "submit"/"press enter", "cancel", "command mode",
  with input buffering (said early, still caught).
- Quick mode: one breath "<start> ... over/send"; code words; the do word
  ("<start> do show commands") for one command without entering command mode.
- Optional timeouts, a default mode, escape words, the response sound.

**Command mode** (`commands.py`, `desktop.py`)
- Windows: switch, open apps, close, minimise/maximise, snap halves and
  corners, move to monitor N, arrange (columns/rows/grid/main/cascade, the
  most even layout per screen), swap, saved layouts, a numbered window list
  with chips on every window.
- Clicking: numbered click labels from UI Automation, click by name, a
  mouse grid, double/right click, type into a label.
- Keys, tabs, scrolling, undo of the last command, edit by voice ("make that
  shorter": rules first, LLM for the rest).
- Learning: "remember that" maps a missed phrase to the last command;
  remembered LLM answers.
- An optional LLM planner for requests the patterns don't cover: it can only
  pick from the same command list, risky plans wait for "yes", and screen
  numbers the user didn't say are dropped.
- Sleep (confirmed by "goodnight"), the night routine (monitors off, the PC
  kept awake), the command sheet ("show commands").

**Look and feel**
- Themes for the pill, labels, window list, command sheet and a paste
  effect: Royal (the default and the identity), Gemstone, Silver, Smooth,
  Fire, Ice and more, plus a theme builder (custom themes in themes.json).
- The Settings window (Appearance, Theme builder, Dictation, Listening,
  Sounds, Camera), applied live; generated cue sounds; the crown icon and
  app identity (notifications and the taskbar say Golden Flow).

**Camera mode, part one** (`camera.py`): which screen you're looking at
(head pose + iris), calibration per screen, a camera pill, the listener
gated while you look away, on/off by voice. Not yet tried with a real face.

**Health and plumbing**: the Ollama watcher with a tray restart and an
orphaned-runner sweep; default-mic following; PortAudio restart; single
instance; start at logon and after sleep; the updater (GitHub releases);
CI on both OSes.

**macOS, stage 1**: everything above except window control, click labels,
the grid and the paste effect (see "Windows and Mac").

## Running it

- It runs **elevated** from Task Scheduler (`localflow install-startup` makes
  the tasks). To restart it: `schtasks /run /tn "LocalFlow resume"`. Don't
  start a second copy from a terminal: there's a single-instance mutex, and a
  console process is not what users run.
- Don't restart it while the user is mid-dictation: check that the last
  lines of `localflow.log` aren't a start phrase without a paste after it.
- `localflow.log` is the record of what happened (rotating, 1 MB x 3). It
  never contains window titles or dictated text, only program names and
  character counts. Keep it that way.
- Config changes from Settings apply live: Settings writes `config.toml`,
  the tray polls its mtime and copies the keys in `tray.LIVE_SETTINGS`.
  Anything not listed there needs a restart.
- Camera support is an optional extra: `uv sync --no-dev --inexact --extra
  camera` (Settings ▸ Camera ▸ Install does this). `install.ps1` syncs with
  `--inexact`, so updates don't remove it.

## Windows and Mac

One package for both. `localflow.platform` says which (`IS_WIN`, `IS_MAC`)
and keeps the Win32 modules importable on a Mac (DLL stand-ins:
configurable, never callable). The Mac's versions live in `localflow.mac`
and replace the Windows functions of the same names at the end of
`desktop.py` and `window.py`; smaller differences are `if IS_MAC:` in
place (inject, sounds, micwatch, hotkey, health, cli, tray, config
defaults).

Mac rules learned the hard way (CI caught each):
- **Tk must start before anything touches AppKit** (`platform.tk_first()`),
  or Tk dies calling its own NSApplication methods on pystray's plain one.
- **Tk and the menu-bar icon share the main thread**: the overlay runs
  there (`Overlay.run_here`) and pystray joins its loop (`run_detached`).
- **No pynput for keys on a Mac**: its layout lookups abort the process off
  the main thread. `mac/keys.py` listens with a Quartz event tap and posts
  key codes. (pynput's mouse is fine.)
- Overlay windows are borderless Tk windows whose NSWindow ignores the
  mouse, floats at the status level and joins every Space (`mac/overlay.py`).
- A Mac "hwnd" in desktop code is `pid << 24 | window number`.
- Start at login is a launchd agent (`mac/startup.py`); `install.sh` /
  `update.sh` follow `install.ps1` / `update.ps1` (same `update.result`).

Stage 2 for the Mac (not built) is planned step by step below.

`.github/workflows/tests.yml` runs on every push, on macos-latest and
windows-latest: every module imports, the no-window suites,
`tests/mac_smoke.py` (overlay windows, screens, apps, clipboard, keys on a
real Mac) and the whole app started, checked and quit on the Mac.

### Mac stage 2: the plan (the next big job)

Every stub is in `src/localflow/mac/desktop.py` (the `_not_yet` lines at
the bottom). Replace them one by one with Accessibility code. The Windows
versions in `desktop.py` define the behaviour and the signatures;
`commands.py` needs no change.

1. **Window handles.** Today a Mac "hwnd" is `pid << 24 | CGWindowNumber`.
   To act on a window you need its `AXUIElement`:
   `AXUIElementCreateApplication(pid)` → `kAXWindowsAttribute`, matched by
   position and size (from `CGWindowListCopyWindowInfo`) or title. Cache the
   match per hwnd; drop it when the window closes.
2. **Move and resize** (`move_windows`, `place`, `center`, `to_monitor`,
   `arrange`, `swap_windows`, `restore`): set `kAXPositionAttribute` and
   `kAXSizeAttribute` with `AXValueCreate(kAXValueCGPointType / CGSizeType)`.
   Coordinates are points, top-left origin across screens (what
   `mac/overlay.screens()` already returns). Keep the Windows version's
   verify-and-correct loop: Mac apps have minimum sizes too. `desktop.grid()`
   and the arrangement styles are pure functions and work as they are.
3. **Minimise, show, close**: `kAXMinimizedAttribute`; close by pressing the
   window's `kAXCloseButtonAttribute` (`AXUIElementPerformAction(...,
   kAXPressAction)`); bring forward with `kAXRaiseAction` plus the app's
   `activateWithOptions_`.
4. **Layouts** (`snapshot` / `restore`): the same JSON as on Windows, keyed
   by the app's bundle path (`_exe_path`) and title.
5. **Click labels** (`controls`, `activate_control`): walk the front
   window's AX tree (`kAXChildrenAttribute`), keep the roles a person clicks
   (AXButton, AXLink, AXMenuItem, AXCheckBox, AXRadioButton, AXTab..., and
   AXTextField for typing), read position and size for the label spot, act
   with `kAXPressAction` (or a click at its centre). Chromium and Electron
   apps need `AXEnhancedUserInterface` set on the app first: the same
   laziness as on Windows.
6. **The caret** (`caret_point`, for the paste effect): the focused
   element's `kAXSelectedTextRangeAttribute`, then
   `kAXBoundsForRangeParameterizedAttribute`.
7. **The grid**: the overlay layers already work on a Mac; it needs only
   `click_at` (done) and the screens.

PyObjC has all of this in `ApplicationServices`
(`pyobjc-framework-ApplicationServices`: add it to the darwin markers in
pyproject.toml). The user must allow **Accessibility** for the Python that
runs Golden Flow: check with `AXIsProcessTrustedWithOptions` and say so in
the log, as `mac/keys.py` does for Input Monitoring. CI can't grant that
permission, so extend `tests/mac_smoke.py` only with what runs without it,
and test the rest on the user's MacBook (TextEdit and Finder windows are
safe targets; never move the user's own windows while testing).

### Camera mode: what's next

docs/CAMERA_MODE.md has the plan, the design rules and the build order.
Part one (attention: which screen) is in `camera.py`: `Watcher` (the
thread), `Attention` (hysteresis), `Calibration`, `decide()`. The listener
asks `_looking_away()` / `_watching()`; the overlay draws the camera pill.
Next, in order: the cover-the-lens kill switch and the open-palm stop
(MediaPipe hand landmarks into a small recognizer with hysteresis, wired to
the listener's cancel and the command runner), then pinch-and-slide and
tilt zoom, then "close that" by gaze. Never open the user's camera without
asking them.

## Recipes (how to change the common things)

- **A new voice command**: add `Command(name, patterns, action, help)` to
  `commands.COMMANDS` (patterns: `[optional]`, `(a|b)`, `{slot}`). Write the
  action as `def _x(arg, ctx) -> str`, returning what the pill should say.
  Add a row to `HELP_SHEET` so it shows on the command sheet, and the ways
  people actually say it to `tests/command_phrases.py`. It is then
  customisable through `command_phrases` with no extra work. Platform calls
  go through `desktop.*` so the Mac gets them too: add a Mac version (or a
  `_not_yet` stub) in `mac/desktop.py` and its `__all__`.
- **A new setting**: a field in the right dataclass in `config.py` (default
  and a comment), a control in `settings.py`, its key in
  `tray.LIVE_SETTINGS` if it should apply live, and a line in
  `config.example.toml`.
- **A new pixel theme**: a `Look(...)` in `pixel.LOOKS` (eleven palette
  roles, a border kind, a dot sprite, a wave style); `themes.PIXEL` builds
  the `Theme` from it. Render with `tools/shot_pixel.py` and look; run
  `tests/pixel_themes.py`. Read `C:\Code\design\GOLDEN_FLOW_DESIGN_STANDARD.md` first.
- **A new legacy theme**: a `Theme(...)` in `themes.py` and an entry in
  `BUILT_IN`. The drawing helpers (`marker`, `label_box`, `paste_burst`)
  take the theme, so the overlay and the Settings preview follow. Render it
  and look.
- **A new overlay layer**: copy an `Overlay._make_*_layer` method. Every
  layer goes through `layers.py` (click-through, no focus, both OSes) and is
  drawn on the overlay's own Tk thread only.
- **A new fixed word list the user says**: add a group to
  `commands.PHRASE_GROUPS` so `phrase_words` can extend it.

## Testing

Each `tests/*.py` is a script ending "all passed" or "N FAILED". The full list
and which need a GPU is in ARCHITECTURE.md ▸ Testing. The rules:

1. **Run the no-window suites after every change**: `learning`, `quick_mode`,
   `settings_apply`, `layouts`, `camera_mode`, `live_stop`, `edit_commands`,
   `enroll_redraw`. They take seconds and need no models or windows.
2. **Run `tests/always_on.py`** (TTS through the real listener, CPU, a few
   minutes) after touching `always_on.py` or `wake.py`.
3. **GUI tests** (`desktop_commands`, `paste_target`, `terminal_paste`,
   `hotkeys`, `layouts --live`) open windows and press keys. Run them only
   when nobody is using the PC *and* nothing is being dictated (voice isn't
   keyboard input, so an idle timer alone isn't enough). Never put them in
   a quick loop with the others.
4. **Keep a control.** A new test for a bug must fail on the old code: put
   the old file back (`git show HEAD:path > path`), run it, restore. Several
   tests here say so in a comment. A test that can't fail proves nothing.
5. On a low-memory PC, set `OPENBLAS_NUM_THREADS=1`, and if the voice suite
   runs out of memory, unload the Ollama model first.
6. Visual changes (themes, labels, icons): render them to an image and look
   (a no-activate topmost Tk window plus `ImageGrab`; DPI-aware). Several
   bugs were only visible this way (labels drawn at 8 pt, an oversized
   preview).

## Releasing

1. Bump `version` in `pyproject.toml` (no BOM).
2. `git add -A`, then a **privacy check of the staged diff**: the repo is
   public. Never commit the user's own start/stop phrases or their earlier
   ones, names, usernames, email, work app names, hardware names, or any of
   the gitignored private files. The user keeps the exact word list; ask
   for it, and grep `git diff --cached` for it before every push. Docs use
   the made-up phrase "Okay listen, Marigold." in examples.
3. Write the commit message to a file and `git commit -F <file>`; end it
   with the `Co-Authored-By` line. Check the commit exists (`git log -1`).
4. Push `main`, `git fetch`, and confirm `origin/main` equals `HEAD`.
5. `git tag vX.Y.Z` on that commit, push the tag, then
   `gh release create vX.Y.Z --verify-tag --notes-file <file>`. Installed
   copies find the release and offer the update.
6. Restart the user's copy (see Running it) and confirm the log shows the
   new version.

Never push the `pre-public-history` branch. Don't push untested code to
`main`.

## How the user works (and likes things)

- Talks to the PC all day; reports problems by dictating, often mid-task.
  Read `localflow.log` before guessing: most reports were explained there
  (what was heard, what matched, what timed out).
- Wants **total voice control**, algorithms first, LLM optional.
- Loves: the command click (it feels like pressing a button with your
  voice), "<start> do show commands", the themes (Royal is the default and
  the identity: JoJo's "Golden Wind"; Gemstone, Fire and Ice are favourites),
  symmetrical icons and marks.
- Asked to be reminded of unbuilt ideas at the end of each piece of work
  (the list is below).
- Has several monitors (one portrait) and plans a webcam per monitor.

## Per-user words (keep everything customisable)

Anything a person says to the app must be changeable by that person, with
the built-ins still working. When you add a command or a phrase list, wire
it into these, not into a new fixed list:

- Start/stop phrases: `wake.py` (set up per user with their voice).
- `always_on.cancel_words` / `exit_words` (escape words).
- `always_on.command_phrases` ("phrase = command", slots allowed) and
  `always_on.commands` (the older dict form) for new ways to say commands.
- `always_on.phrase_words` ("group = phrase") for the built-in groups in
  `commands.PHRASE_GROUPS`. Add a new group there if you add a new fixed
  list of words the user says.
- `always_on.do_words`, `quick_stop`, `quick_send`, `code_words`.
- Learned phrases (`learned.json`, "remember that").
- New settings that should apply without a restart go in
  `tray.LIVE_SETTINGS` and `Tray.apply_settings`.

The listener is English-only (`base.en`, `language="en"` in
`Detector.words`, English patterns). Supporting another language means a
multilingual listening model, the language passed through, and patterns
or user phrases in that language.

## Things that were hard (read before touching)

- **Matching speech**: everything goes through fuzzy word alignment
  (`wake._aligned`, `commands.Pattern`). Small models mishear short words;
  the fixes were second listens with the bigger model, the live-preview stop
  check, joining a phrase split by a pause, and accepting known mishearings
  only where a real command follows (the do word's "to"/"undo").
- **Time**: everything in the listener is in absolute samples (16 kHz) of
  the ring buffer. `_consumed` stops a phrase being used twice. Commands
  after the stop phrase are held until the text is pasted (`BUFFER`), and
  the window opens where the phrase ended, not when it was noticed.
- **Moving other apps' windows**: never trust one SetWindowPos. Apps
  rescale and reposition themselves (system-DPI-aware WinForms on mixed
  scaling) or have minimum sizes; use `desktop.move_windows`, which checks
  and corrects. Test moves against models (tests/layouts.py), not the
  user's windows: a "no-op" move of a real window during debugging moved it.
- **Windows and DPI**: every overlay layer is a no-activate, click-through
  Tk Toplevel; Win32 calls need argtypes; UI Automation coordinates need
  `per_monitor_dpi()` on the calling thread.
- **Tk fonts**: negative sizes are pixels. A `max(8, px)` on a negative size
  once drew every label at 8 pt.
- **Chromium apps** build their accessibility tree only when asked: the
  first look at a window queries twice.
- **One Tk per process**, on its own thread for the overlay. Settings,
  History, phrase setup and calibration are separate processes; they set
  the app identity first so the taskbar says Golden Flow.

## Version history (short)

| Version | What landed |
|---|---|
| 1.0-1.10 | Dictation, cleanup (LLM and rules), phone app, always-on phrases, updater, paste where you started, command mode, click labels, undo. |
| 1.11-1.16 | Window list, monitors, learning from misses, chips, grid, typing into labels, mode switch. |
| 1.17-1.19 | Ollama health, response sound, default mode, cancel after the stop phrase, timeouts, remembered LLM answers, edit by voice, Settings window, themes. |
| 1.20-1.22 | Royal (default), Gemstone, Smooth, Silver and more; command sheet; escape words. |
| 1.23 | Fire and Ice; themed labels; arrange windows; saved layouts; the command sheet bug fix. |
| 1.24 | Quick mode, code words, input buffering; notifications named; readable labels; more clickables; chips for every window. |
| 1.25 | Command click, Preview theme, live restyle, camera design doc. |
| 1.26 | Camera mode part one; arrangement styles; the do word always on. |
| 1.27 | Golden Flow name and crown emblem; paste effects; camera pill; camera by voice; this handoff. |
| 1.28 | Command mode hears with the dictation model; commands wait for "on monitor N"; your own words for every phrase group and command; PortAudio restart fix. |
| 1.29 | Windows that move themselves or keep a minimum size are moved, checked and corrected (mixed-DPI desks); swap two windows; "command mode" split by a pause, or pasted as text, now switches; "which screen?" instead of an LLM guess. |
| 1.30 | macOS: dictation, always-on, overlay and themes, menu bar, Settings, command mode basics; CI on a real Mac. |
| 1.31 | The night routine: "night time routine" / "monitors off" turns every monitor off and keeps the PC awake until someone's back. |
| 1.31.1 | Ollama's orphaned model runners (5-6 GB each, left when its server is killed or updated) are found and killed; restarting Ollama kills the whole tree. Golden Flow's own ~4 GB is the CUDA + two Whisper models baseline (measured fresh), not a leak. |
| 1.32.0 | The pill no longer vanishes while you click around: it re-asserts its place on top twice a second (another topmost window, e.g. a call bar or security popup, used to bury it for good) and reappears if the system hides it (tests/pill_stays_on_top.py, with a control). Also merges the downstairs PC's phrase-sensitivity slider (always_on.voice_sensitivity). |
| 1.32.1 | "Cancel" after the stop phrase now stops the work at once: Engine._abortable abandons a running cleanup/style call instead of waiting it out (up to 20 s) and discarding the result; the raw words still go to history (tests/cancel_midway.py, with a control). The abandoned Ollama request finishes on its own, so an immediate re-dictation can queue behind it briefly. |
| 1.33.0 | **Pixel themes** (the house style in C:/Code/design): Pixel Block, Knight, Wizard, Electric, drawn by `pixel.py` (Silkscreen text, segmented border, sprites, LED/blade/rune/bolt meters); default on. `overlay.pixel` / `overlay.pixel_theme`; unticking returns to the untouched legacy v2 themes. Settings > Appearance: tick + four live tiles. Pixel crown icon (`icon.pixel_crown`, nearest-neighbour .ico). Click labels, window list, command sheet and paste burst follow the pixel look. Tests: `tests/pixel_themes.py` (real overlay via PrintWindow, with a control). Standard: `C:/Code/design/GOLDEN_FLOW_DESIGN_STANDARD.md`. |
| 1.33.1 | The pixel pill was too big (about as wide as the legacy one but twice as tall, text huge). Art pixel `cell(s)` 2.7 -> 1.9 x scale, row 14 -> 13 art px, width 92 -> 76, live-text line pitch 8 -> 7: about 40% smaller each way, still Silkscreen at whole art pixels. Settings tiles now draw the pill at its true size. |
| 1.33.2 | Legacy themes are the default again; pixel themes are an opt-in tick (`overlay.pixel = false`). Reported "can't switch to a pixel theme": the running app was still pre-1.33 code (Settings is a new process, the pill is not), so it ignored the pixel keys until restarted. |
| 1.34.0 | Feedback round on the pixel themes. Pill row 13 -> 14 art px and meters 9 -> 7 tall, so the meter clears the border (knight merlons now one row deep). Knight: pointed shield mark, swords as bars (point, blade, guard, red grip, pommel; pitch 4). Electric: jagged one-pixel lightning. Six new looks (ten in all): royal (crown, ruby marquee, jewel pillars), dark (moon), light (sun), leaf (leaf storm, vines), dragon (blue/purple flames), legal (gavel, stacked papers). The app icon is now always the pixel crown. Settings wears the picked look (palette + Silkscreen headings/tabs/buttons; `_set_palette`/`_restyle`, live on pick) and `tools/shot_settings.py` renders it by PrintWindow. |
| 1.34.1 | Feedback on 1.34.0. Live text now has `TEXT_PAD` = 5 rows under the top edge (it touched the border, worst on Knight). Settings tabs in Silkscreen at 16 x dpi (`_size_tabs` shrinks them only if the screen is too narrow, and widens the window). Royal: gold only (no red) with crown points above the pill (`CRESTS`, drawn by `_crest`; the window has room via `TEXT_U`) and gold jewel pillars. Dragon: every border segment and every flame tongue is a different flame colour; flames lick above the pill. Legal rebuilt to look serious: navy and brass, a calm brass rule with one slow glint, scales of justice mark, courthouse-column meter (no seal, no papers). |

## What's next (the user wants these pitched at the end of each job)

Roughly in the order the user asked for them:

- **The Mac's stage 2** (window control, click labels, the caret): the plan
  is under "Windows and Mac" above.

- Camera mode next steps, in order: cover-the-lens stop and open-palm stop;
  pinch-and-slide and tilt zoom; "close that" / "move this" with gaze or
  pointing; micro-gestures (thumbs up/down to confirm first); presence and
  auto-lock; eye-tracker support. Details in docs/CAMERA_MODE.md.
- Talking back: read things aloud (a confirmation, a selected text, the
  answer to a question). Low priority for this user, useful for others.
- Search the command sheet by voice ("show commands for windows").
- Theme by time of day.
- Lock, restart and shut down by voice.
- Enhance the night-time routine: personal routines, e.g. only certain
  windows stay up, peripherals' lights off. Tonight's version only turns
  the monitors off and keeps the PC awake.
- Switch a saved layout automatically when a monitor is plugged in or out.
- The rest of the rename (package, repo, tasks) if and when it's wanted.

## Open issues

- Camera mode hasn't been tried with a real face yet (no camera was opened
  without the user's say-so); the first calibration will show how well one
  webcam separates screens.
- A work PC copy has missed sound cues before; it needs that PC's log.
- Mac stage 1 passed CI (imports, suites, overlay windows, a full start and
  quit), but nobody has dictated on a real Mac yet: the first run will show
  the permission prompts, the pill and the menu-bar icon.
- Memory: Golden Flow's ~4.1 GB private is CUDA plus two Whisper models,
  mostly paged out (working set ~100 MB). Ollama's model (~5.7 GB) stays
  loaded for `cleanup.keep_alive` (30 min). See ARCHITECTURE.md.
- See ARCHITECTURE.md ▸ Known issues for older ones.
