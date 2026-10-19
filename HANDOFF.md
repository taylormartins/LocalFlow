# Handoff: Golden Flow (the `localflow` package)

For the next developer or agent. Read this first, then
[ARCHITECTURE.md](ARCHITECTURE.md) for how each part works and the traps,
[README.md](README.md) for what users see, and
[docs/CAMERA_MODE.md](docs/CAMERA_MODE.md) for the camera plan.

Written 2026-10-18, at v1.27.0; updated for v1.30.0.

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
| `themes.py` | Theme data and every drawing function the overlay and Settings share. |
| `settings.py` | The Settings window (its own process): Appearance, Theme builder, Dictation, Listening, Sounds, Camera. |
| `tray.py` | The tray icon and menu; applies Settings changes live (`LIVE_SETTINGS`). |
| `camera.py` | Camera mode: the watcher thread, gaze geometry, calibration. |
| `icon.py` | The crown emblem, window icons, the app identity (notifications say Golden Flow). |
| `enroll.py` | Phrase setup window. |
| `history.py` | Dictation history window. |
| `sounds.py` | Generated cue sounds (tick, pop, click...). |
| `server.py`, `pwa/` | Phone dictation over HTTPS. |
| `updates.py` | Version check against GitHub releases. |

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

Stage 2 for the Mac (not built): window control through Accessibility
(AXUIElement: move, resize, minimise, close, arrange, layouts, swap), click
labels from the AX tree, the caret for the paste effect.

`.github/workflows/tests.yml` runs on every push, on macos-latest and
windows-latest: every module imports, the no-window suites,
`tests/mac_smoke.py` (overlay windows, screens, apps, clipboard, keys on a
real Mac) and the whole app started, checked and quit on the Mac.

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
| 1.30 | macOS: dictation, always-on, overlay and themes, menu bar, Settings, command mode basics; CI on a real Mac. |
| 1.29 | Windows that move themselves or keep a minimum size are moved, checked and corrected (mixed-DPI desks); swap two windows; "command mode" split by a pause, or pasted as text, now switches; "which screen?" instead of an LLM guess. |
| 1.28 | Command mode hears with the dictation model; commands wait for "on monitor N"; your own words for every phrase group and command; PortAudio restart fix. |

## Ideas not built yet (the user wants these pitched)

- Camera mode next steps, in order: cover-the-lens stop and open-palm stop;
  pinch-and-slide and tilt zoom; "close that" / "move this" with gaze or
  pointing; micro-gestures (thumbs up/down to confirm first); presence and
  auto-lock; eye-tracker support. Details in docs/CAMERA_MODE.md.
- Talking back: read things aloud (a confirmation, a selected text, the
  answer to a question). Low priority for this user, useful for others.
- Search the command sheet by voice ("show commands for windows").
- Theme by time of day.
- Lock, restart and shut down by voice.
- Switch a saved layout automatically when a monitor is plugged in or out.
- The rest of the rename (package, repo, tasks) if and when it's wanted.
- The Mac's stage 2: window control and click labels through Accessibility.

## Open issues

- Camera mode hasn't been tried with a real face yet (no camera was opened
  without the user's say-so); the first calibration will show how well one
  webcam separates screens.
- A work PC copy has missed sound cues before; it needs that PC's log.
- See ARCHITECTURE.md ▸ Known issues for older ones.
