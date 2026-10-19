"""Command line entrypoint."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from . import APP_NAME
from .platform import IS_MAC
from . import config as config_mod


def _specs(value: str | list[str]) -> list[str]:
    """A hotkey setting is one spec or a list of them; "" disables it."""
    items = [value] if isinstance(value, str) else list(value)
    return [x for x in items if x]


def _detach_hidden_console() -> None:
    """Drop the invisible console the tray app is born with, and write to NUL.

    The venv's pythonw.exe is a launcher that starts the console python.exe, so
    the "windowless" tray app still owns a hidden conhost that nobody reads.
    Every print, log line, uvicorn access line and tqdm bar goes to it. On
    2026-10-11, soon after the Tk overlay arrived, a WriteConsoleW to it blocked
    forever. The hotkey worker was stuck mid-log-line, F9 died, and the process
    otherwise looked healthy. With no console attached, nothing can block on one.
    The log file keeps everything.

    Keyed on how we were launched, not on the console window: on this Windows 11
    box the hidden console's HWND reports IsWindowVisible() == True even though
    nothing is on screen. Launched as pythonw.exe means "no console wanted";
    `uv run localflow run` from a terminal keeps its output.
    """
    if os.name != "nt" or not sys.executable.lower().endswith("pythonw.exe"):
        return
    import ctypes

    ctypes.windll.kernel32.FreeConsole()
    devnull = open(os.devnull, "w", encoding="utf-8")
    # Point the C-level fds at NUL as well, so native libraries that write to
    # fd 1/2 directly get a sink instead of a dead handle.
    for fd in (1, 2):
        try:
            os.dup2(devnull.fileno(), fd)
        except OSError:
            pass
    sys.stdout = sys.stderr = devnull
    sys.stdin = open(os.devnull, "r", encoding="utf-8")


def _setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        # Launched at login there is no console, so without this a failure is
        # completely invisible. Rotating, because it runs all day every day.
        from logging.handlers import RotatingFileHandler

        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(fmt)
            root.addHandler(handler)
        except OSError as exc:
            root.warning("could not open log file %s: %s", log_file, exc)

        # A native crash (abort() inside a C++ library, code 0xC0000409) kills the
        # process before logging can say anything; on 2026-10-01 the app just
        # vanished mid-transcription. faulthandler dumps every thread's Python
        # stack on the way down. Separate file because rotation would close the
        # handle faulthandler writes to. Kept open for the life of the process.
        import faulthandler

        try:
            crash_file = open(log_file.with_name("crash.log"), "a", encoding="utf-8")
            crash_file.write(f"--- started {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()}\n")
            crash_file.flush()
            faulthandler.enable(crash_file, all_threads=True)
        except OSError as exc:
            root.warning("could not open crash log: %s", exc)

    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)


def cmd_devices(args, cfg) -> int:
    from .audio import list_input_devices, resolve_device

    devices = list_input_devices()
    if not devices:
        print("No input devices found.")
        return 1

    print("Input devices:\n")
    for d in devices:
        print(f"  {d}")

    print()
    try:
        chosen = resolve_device(cfg.audio.device)
        label = f"audio.device = {cfg.audio.device!r}" if cfg.audio.device else "system default"
        print(f"Selected ({label}):\n  {chosen or 'system default'}")
    except Exception as exc:
        print(f"Selection failed: {exc}")
        return 1
    return 0


def cmd_check(args, cfg) -> int:
    """Verify every moving part before trusting the thing with real work."""
    ok = True

    print("== GPU / CTranslate2 ==")
    try:
        from . import _cuda

        dirs = _cuda.register()
        print(f"  bundled CUDA dirs: {len(dirs)}")
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
        print(f"  ctranslate2 {ctranslate2.__version__}, CUDA devices: {count}")
        if count == 0:
            print("  ! no CUDA device visible; will fall back to CPU")
            ok = False
    except Exception as exc:
        print(f"  ! {exc}")
        ok = False

    print("\n== Speech model ==")
    from . import models

    name = cfg.whisper.model
    folder = models.sideloaded(name)
    if folder:
        print(f"  {name}: sideloaded at {folder}")
    else:
        try:
            from faster_whisper.utils import download_model

            download_model(name, local_files_only=True, cache_dir=cfg.whisper.download_root or None)
            print(f"  {name}: downloaded")
        except Exception:
            kind, detail = models.diagnose(name)
            if kind == "ok":
                print(f"  {name}: not downloaded yet; it downloads on first use ({detail})")
            else:
                print(f"  ! {name}: not downloaded, and the download is blocked:")
                print(f"    {detail}")
                print(f"    {models.OFFLINE_HELP}")
                ok = False

    print("\n== Microphone ==")
    try:
        from .audio import resolve_device

        print(f"  {resolve_device(cfg.audio.device) or 'system default'}")
    except Exception as exc:
        print(f"  ! {exc}")
        ok = False

    print("\n== Cleanup ==")
    if not cfg.cleanup.enabled:
        print("  disabled in config")
    elif cfg.cleanup.method == "rules":
        print("  algorithmic (no LLM) - nothing to check")
    else:
        from .cleanup import Cleaner

        cleaner = Cleaner(cfg.cleanup, cfg.vocabulary)
        if cleaner.available():
            import httpx

            try:
                tags = httpx.get(f"{cfg.cleanup.host}/api/tags", timeout=5).json()
                names = {m["name"] for m in tags.get("models", [])}
                mark = "ok" if cfg.cleanup.model in names else "! NOT PULLED"
                print(f"  server up, {len(names)} models; {cfg.cleanup.model}: {mark}")
                if cfg.cleanup.model not in names:
                    print(f"    run: ollama pull {cfg.cleanup.model}")
                    print("    until then, algorithmic cleanup is used instead")
            except Exception as exc:
                print(f"  ! {exc}")
                ok = False
        else:
            # Not fatal: every dictation falls back to the algorithmic cleanup.
            print(f"  ! cannot reach Ollama at {cfg.cleanup.host}")
            print('    algorithmic cleanup is used instead (or set cleanup.method = "rules")')
        cleaner.close()

    print("\n== Hotkeys ==")
    from .hotkey import parse_spec

    for name, spec in [("dictate", x) for x in _specs(cfg.hotkeys.dictate)] + [
        ("dictate_raw", x) for x in _specs(cfg.hotkeys.dictate_raw)
    ]:
        try:
            parse_spec(spec)
            print(f"  {name}: {spec}")
        except ValueError as exc:
            print(f"  ! {exc}")
            ok = False

    print("\n== Server ==")
    if cfg.server.enabled:
        state = "set" if cfg.server.token else "! EMPTY (unauthenticated)"
        print(f"  {cfg.server.host}:{cfg.server.port}, token: {state}")
    else:
        print("  disabled")

    print("\n" + ("All checks passed." if ok else "Some checks failed - see ! lines above."))
    return 0 if ok else 1


def cmd_transcribe(args, cfg) -> int:
    """Transcribe an audio file. Exercises the pipeline without a microphone."""
    import numpy as np

    from .audio import decode_upload
    from .engine import Engine

    path = Path(args.path)
    if not path.exists():
        print(f"no such file: {path}")
        return 1

    if args.rules:
        cfg.cleanup.method = "rules"
    engine = Engine(cfg)
    audio = decode_upload(path.read_bytes())
    print(f"decoded {len(audio) / 16000:.2f}s of audio")
    if audio.size == 0:
        return 1
    if args.boost:
        cfg.audio.boost = args.boost
        audio, gain = engine.recorder.apply_gain(audio)
        print(f"mic boost {args.boost}: x{gain:.1f}")

    result = engine.process(np.asarray(audio), cleanup=not args.raw, source="file")
    print(f"\nraw:     {result.raw}")
    print(f"cleaned: {result.text}")
    print(
        f"\nwhisper {result.transcribe_seconds:.2f}s, "
        f"cleanup {result.cleanup_seconds:.2f}s, {result.speedup:.0f}x realtime"
    )
    engine.shutdown()
    return 0


def cmd_clean(args, cfg) -> int:
    """Run the cleanup on typed text: the fastest way to see what the rules do."""
    from .cleanup import Cleaner

    if not args.llm:
        cfg.cleanup.method = "rules"
    cleaner = Cleaner(cfg.cleanup, cfg.vocabulary)
    started = time.perf_counter()
    text = cleaner.clean(args.text)
    print(text)
    print(f"\n({cleaner.last_outcome}, {(time.perf_counter() - started) * 1000:.1f} ms)")
    cleaner.close()
    return 0


def cmd_fetch_model(args, cfg) -> int:
    """Download the Whisper model now, optionally as a zip for another machine."""
    from . import models

    name = args.model or cfg.whisper.model
    try:
        if args.zip:
            out = Path(args.zip)
            if out.suffix.lower() != ".zip":
                out = out / f"localflow-model-{name}.zip"
            models.export_zip(name, out)
            print(f"Saved {out} ({out.stat().st_size / 1e6:.0f} MB)")
            print("On the other PC:  install.cmd -ModelZip <path to this zip>")
            print("             or:  uv run localflow import-model <path to this zip>")
        else:
            cache = cfg.whisper.download_root or None
            try:
                path = models.sideloaded(name) or models.download(name, cache_dir=cache)
            except models.ModelUnavailable as exc:
                # A model LocalFlow doesn't mirror (large-v3-turbo, on a GPU PC)
                # behind a block: a mirrored one beats no transcription at all.
                fallback = args.fallback_model
                if not fallback or fallback == name or name in models.GITHUB_MODELS:
                    raise
                print(f"! {exc}")
                print(f"\nFalling back to {fallback}, which LocalFlow mirrors.")
                path = models.sideloaded(fallback) or models.download(fallback, cache_dir=cache)
                config_mod.save_setting("whisper", "model", fallback, args.config)
                print(f"Set whisper.model = {fallback!r} in config.toml (was {name!r})")
                name = fallback
            print(f"{name} ready at {path}")
    except models.ModelUnavailable as exc:
        print(f"! {exc}")
        return 2
    return 0


def cmd_import_model(args, cfg) -> int:
    """Install a model from a zip or folder (the offline install path)."""
    from . import models

    try:
        name, dest = models.import_model(Path(args.path), args.name)
    except (OSError, ValueError) as exc:
        print(f"! {exc}")
        return 1
    print(f"Installed {name} at {dest}")
    if cfg.whisper.model != name:
        config_mod.save_setting("whisper", "model", name, args.config)
        print(f"Set whisper.model = {name!r} in config.toml (was {cfg.whisper.model!r})")
    return 0


def cmd_history(args, cfg) -> int:
    """Search past dictations; paste or copy one (opens a window)."""
    from .history import main as history_main

    return history_main(cfg)


def cmd_settings(args, cfg) -> int:
    """The Settings window (themes, sounds, timeouts, cleanup...)."""
    from .settings import main as settings_main

    return settings_main(cfg)


def cmd_camera_calibrate(args, cfg) -> int:
    """Camera mode: look at a dot on each screen in turn (opens windows)."""
    from .camera import calibrate

    msg = calibrate(cfg.camera)
    print(msg)
    return 0 if msg.startswith("Calibrated") else 1


def cmd_enroll(args, cfg) -> int:
    """Set up the always-on start/stop phrases (opens a window)."""
    from .enroll import main as enroll_main

    return enroll_main(cfg)


def cmd_listen(args, cfg) -> int:
    """Always-on mode in the console: shows what it hears and catches, and the
    finished dictation, but pastes nothing. The way to try phrases out."""
    import numpy as np

    from .always_on import Listener
    from .engine import Engine

    if args.rules:
        cfg.cleanup.method = "rules"
    engine = Engine(cfg)
    stamp = lambda: time.strftime("%H:%M:%S")  # noqa: E731

    def event(kind: str, text: str) -> None:
        if kind == "heard" and not args.quiet:
            print(f"{stamp()}  heard: {text}")
        elif kind == "start":
            print(f"{stamp()}  >>> START phrase ({text}) - dictating")
        elif kind == "stop":
            print(f"{stamp()}  <<< STOP phrase ({text})")
        elif kind == "near":
            print(f"{stamp()}  ??? close but not accepted: {text}")
        elif kind == "command-mode":
            print(f"{stamp()}  === command mode {text}")
        elif kind == "ran":
            print(f"{stamp()}  ->  command: {text} (not run: listen mode pastes and presses nothing)")
        elif kind == "unknown":
            print(f"{stamp()}  ??? not a command: {text}")
        elif kind == "confirm":
            print(f"{stamp()}  ... {text}: waiting for the confirmation word")
        elif kind == "plan":
            print(f"{stamp()}  LLM plan: {text}")

    engine.on_result(lambda r: print(f"{stamp()}  DICTATION: {r.text!r}  (raw: {r.raw!r})"))
    listener = Listener(engine, cfg, inject=False, on_event=event)
    if not listener.ready:
        print("No phrases set up yet: run `uv run localflow enroll` first.")
        return 1
    p = listener.detector.phrases
    print(f"start phrase: {p.start.text!r}  (accepts: {'; '.join(p.start.variants)})")
    print(f"stop phrase:  {p.stop.text!r}  (accepts: {'; '.join(p.stop.variants)})")
    print("loading models...")
    listener.detector.load()
    engine.transcriber.load()

    if args.file:
        from .audio import decode_upload

        audio = decode_upload(Path(args.file).read_bytes())
        print(f"playing {len(audio) / 16000:.1f}s from {args.file}\n")
        listener.run_file(np.asarray(audio, dtype=np.float32))
        # finish_external hands the pipeline to a thread; let it complete.
        for _ in range(600):
            if engine.state.value == "idle" and not engine.recording:
                break
            time.sleep(0.1)
        time.sleep(0.5)
        return 0

    listener.start()
    print("\nListening. Say the start phrase, dictate, then the stop phrase. Ctrl+C to quit.\n")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        time.sleep(0.3)
    return 0


def cmd_record(args, cfg) -> int:
    """Record from the mic for N seconds and transcribe. Console-only smoke test."""
    from .engine import Engine

    if args.rules:
        cfg.cleanup.method = "rules"
    engine = Engine(cfg)
    engine.transcriber.load()

    print(f"Recording {args.seconds}s from the microphone - speak now...")
    engine.recorder.start()
    time.sleep(args.seconds)
    audio = engine.recorder.stop()

    if audio.size == 0:
        print("Nothing captured. Check the input device and that the mic is unmuted.")
        return 1

    peak = float(abs(audio).max())
    print(f"captured {len(audio) / 16000:.2f}s, peak level {peak:.3f}")
    if peak < 0.01:
        print("! signal is nearly silent - check the mic gain (and INST/LINE switch on an interface)")

    result = engine.process(audio, cleanup=not args.raw, source="record")
    print(f"\nraw:     {result.raw}")
    print(f"cleaned: {result.text}")
    engine.shutdown()
    return 0


def cmd_level(args, cfg) -> int:
    """Live input meter. The fastest way to tell a dead mic from a dead pipeline."""
    import numpy as np
    import sounddevice as sd

    from .audio import QUIET_PEAK, SILENCE_PEAK, resolve_candidates

    candidates = resolve_candidates(cfg.audio.device)
    device = candidates[0] if candidates else None
    print(f"Reading {device or 'system default'}")
    print("Shows the mic itself, before Mic boost. Speak normally. Ctrl+C to stop.\n")

    rate = int(device.default_samplerate) if device else 44100
    peak_seen = 0.0
    try:
        with sd.InputStream(
            device=device.index if device else None,
            channels=1,
            samplerate=rate,
            dtype="float32",
        ) as stream:
            while True:
                block, _ = stream.read(int(rate * 0.1))
                block = block.reshape(-1)
                if cfg.audio.gain != 1.0:
                    block = np.clip(block * cfg.audio.gain, -1.0, 1.0)
                peak = float(np.abs(block).max())
                peak_seen = max(peak_seen, peak)

                # Log scale: speech sits far too close to zero to see linearly.
                db = 20 * np.log10(peak) if peak > 1e-6 else -100.0
                filled = int(max(0.0, min(1.0, (db + 60) / 60)) * 40)
                verdict = "silent" if peak < SILENCE_PEAK else "quiet " if peak < QUIET_PEAK else "good  "
                print(f"\r[{'#' * filled}{'-' * (40 - filled)}] {db:6.1f} dB  {verdict}", end="", flush=True)
    except KeyboardInterrupt:
        pass

    print(f"\n\nLoudest peak seen: {peak_seen:.4f}")
    if peak_seen < SILENCE_PEAK:
        print("  -> No signal. Check the mic gain, the 48V switch if your mic")
        print("     needs phantom power, the cable, and Windows mic privacy settings.")
        return 1
    from .audio import AUTO_MAX, BOOSTS

    boost = (cfg.audio.boost or "none").lower()
    if peak_seen < QUIET_PEAK:
        # The smallest fixed boost that lifts this to a comfortable level.
        fits = [b for b in ("low", "medium", "high") if peak_seen * BOOSTS[b] >= 0.2]
        pick = fits[0] if fits else "auto"
        print("  -> Too quiet for Whisper's VAD, which will discard some of it.")
        print("     Turn up the mic's own gain if it has one. Otherwise set Mic boost")
        print(f"     in the tray menu to {pick.capitalize()} or Auto (now: {boost.capitalize()}).")
        if peak_seen * AUTO_MAX < 0.2:
            print("     Even Auto (+26 dB) leaves this quiet - check the mic itself.")
        return 0 if boost != "none" else 1
    print("  -> Healthy. Speech should transcribe fine.")
    return 0


def _startup_shortcut_path() -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "LocalFlow.lnk"
    )


def cmd_install_startup(args, cfg) -> int:
    """Create (or remove) a Startup shortcut so the tray app runs at login
    (on a Mac: a launchd agent)."""
    from .config import PROJECT_ROOT

    if IS_MAC:
        from .mac import startup

        if args.remove:
            print("Removed" if startup.remove() else "Not installed; nothing to remove.")
            return 0
        path = startup.install(PROJECT_ROOT)
        print(f"Installed {path}: {APP_NAME} starts at login, and it's running now.")
        print("To undo: uv run localflow install-startup --remove")
        return 0

    link = _startup_shortcut_path()

    if args.remove:
        if link.exists():
            link.unlink()
            print(f"Removed {link}")
        else:
            print("Not installed; nothing to remove.")
        return 0

    pythonw = PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        print(f"No virtualenv at {pythonw}\nRun `uv sync` first.")
        return 1

    # A .lnk needs a real .ico; reuse the tray artwork so it is recognisable.
    icon = PROJECT_ROOT / "src" / "localflow" / "pwa" / "localflow.ico"
    if not icon.exists():
        try:
            from .tray import STATE_COLOURS, _icon
            from .engine import State

            _icon(STATE_COLOURS[State.IDLE]).save(
                icon, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)]
            )
        except Exception:
            icon = None

    import os

    import win32com.client

    # restart.vbs -> restart.ps1, not pythonw directly: that script supervises
    # the app (relaunch on crash) and starts it with no console window at all.
    launcher = PROJECT_ROOT / "restart.vbs"
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(str(link))
    shortcut.TargetPath = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")
    shortcut.Arguments = f'"{launcher}"'
    shortcut.WorkingDirectory = str(PROJECT_ROOT)
    shortcut.Description = f"{APP_NAME}: voice control and dictation (tray app)"
    if icon and Path(icon).exists():
        shortcut.IconLocation = str(icon)
    shortcut.save()

    print(f"Installed {link}")
    print(f"  target : wscript {launcher}")
    print(f"  workdir: {PROJECT_ROOT}")
    print(f"  logs   : {PROJECT_ROOT / 'localflow.log'}")
    print("\nIt will start at your next login. To start it now without logging out:")
    print(f"  wscript \"{launcher}\"")
    print("\nTo undo: uv run localflow install-startup --remove")
    return 0


def cmd_serve(args, cfg) -> int:
    """Run only the HTTP server (no hotkeys, no tray)."""
    from .engine import Engine
    from .server import serve

    engine = Engine(cfg)
    engine.warmup()
    print(f"Serving on http://{cfg.server.host}:{cfg.server.port}")
    if not cfg.server.token:
        print("! no server.token set - anyone on the network can use this")
    try:
        serve(engine)
    except KeyboardInterrupt:
        pass
    finally:
        engine.shutdown()
    return 0


def build_bindings(cfg, engine) -> list:
    """Hotkey bindings from cfg.hotkeys. Rebuilt whenever the tray changes keys."""
    from .hotkey import Binding

    bindings = []
    # Each action can have several shortcuts (e.g. Caps Lock and Ctrl+Win);
    # sharing a group makes them one switch, so either can stop the recording.
    for spec in _specs(cfg.hotkeys.dictate):
        bindings.append(
            Binding(
                spec=spec,
                on_press=lambda: engine.start_recording(cleanup=True),
                on_release=lambda: engine.stop_recording(),
                mode=cfg.hotkeys.mode,
                group="dictate",
            )
        )
    for spec in _specs(cfg.hotkeys.dictate_raw):
        bindings.append(
            Binding(
                spec=spec,
                on_press=lambda: engine.start_recording(cleanup=False),
                on_release=lambda: engine.stop_recording(),
                mode=cfg.hotkeys.mode,
                group="dictate_raw",
            )
        )
    if cfg.hotkeys.cancel:
        # Matters most in toggle mode, where a recording left running has no
        # other way out. Never suppressed - swallowing Escape globally would
        # break every other application.
        bindings.append(
            Binding(
                spec=cfg.hotkeys.cancel,
                on_press=lambda: engine.cancel_recording(),
                mode="action",
                suppress=False,
            )
        )
    return bindings


def _hint(cfg) -> str:
    keys = _specs(cfg.hotkeys.dictate)
    if not keys:
        return "No dictation key set - pick one: tray menu > Dictation keys"
    if cfg.hotkeys.mode == "hold":
        hint = f"Hold {' or '.join(keys)} to dictate"
        if _specs(cfg.hotkeys.dictate_raw):
            hint += f", {' or '.join(_specs(cfg.hotkeys.dictate_raw))} for raw"
    else:
        hint = f"Tap {' or '.join(keys)} to start, tap again to stop"
        if cfg.hotkeys.cancel:
            hint += f". {cfg.hotkeys.cancel} cancels"
    return hint


def _update_outcome() -> tuple[str, str] | None:
    """(kind, message) about an update that just ran, read once at startup.

    update.ps1 leaves .updated just before it restarts us, so the new copy
    can say "Updated to X" even when the version number didn't change. A
    failure is in update.result; it's only news for a few minutes.
    """
    from .updates import current, install_incomplete, installed

    root = config_mod.PROJECT_ROOT
    marker = root / ".updated"
    if install_incomplete():
        marker.unlink(missing_ok=True)
        return "error", (f"{APP_NAME} {current()} isn't fully installed (still {installed()}) - "
                         "tray > Finish installing")
    if marker.exists():
        marker.unlink(missing_ok=True)
        return "ok", f"Updated to {APP_NAME} {current()}"
    result = root / "update.result"
    try:
        if time.time() - result.stat().st_mtime < 15 * 60:
            code, _, msg = result.read_text(encoding="ascii").strip().partition(" ")
            if code == "1":
                return "error", f"The update failed: {msg}"
    except OSError:
        pass
    return None


def _version_change() -> str:
    """The new version if this is the first run since an update, else ""."""
    from .updates import current

    try:
        now = current()
        seen_file = config_mod.PROJECT_ROOT / ".version"
        seen = seen_file.read_text(encoding="ascii").strip() if seen_file.exists() else ""
        if seen != now:
            seen_file.write_text(now, encoding="ascii")
        return now if seen and seen != now else ""
    except Exception:
        return ""


_instance_handle = None  # held for the life of the process


def _claim_single_instance(wait: float = 8.0) -> bool:
    """One tray app per session. A named mutex lives exactly as long as the
    process that holds it, so a crashed copy never blocks the next one.

    The last line of defence against stacked tray icons: if a restart ever
    fails to stop the old copy (it ran elevated and the restart didn't), the
    new one bows out instead of adding a second live icon. It waits a few
    seconds first, because during a normal restart the old copy is still on
    its way out.
    """
    global _instance_handle
    if IS_MAC:
        # A lock on a file: held exactly as long as this process lives.
        import fcntl

        from .config import PROJECT_ROOT

        _instance_handle = open(PROJECT_ROOT / ".localflow.lock", "w")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(_instance_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.5)
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    deadline = time.monotonic() + wait
    while True:
        handle = kernel32.CreateMutexW(None, False, "Local\\LocalFlowTrayApp")
        err = ctypes.get_last_error()
        # 183 = already exists; a NULL handle (access denied) means an
        # elevated copy owns it and we're not elevated: also "exists".
        if handle and err != 183:
            _instance_handle = handle
            return True
        if handle:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _watch_quit_file(tray) -> None:
    """restart.ps1 asks the running copy to quit by creating `.quit` (works
    across elevation, unlike signals). Quitting through the tray removes the
    icon; being killed leaves it behind until the mouse passes over it."""
    quit_file = config_mod.PROJECT_ROOT / ".quit"
    quit_file.unlink(missing_ok=True)  # a leftover from before we started

    def watch() -> None:
        while True:
            time.sleep(0.5)
            if quit_file.exists():
                logging.getLogger("localflow").info("restart requested a clean exit")
                try:
                    quit_file.unlink(missing_ok=True)
                except OSError:
                    pass
                tray.stop()
                return

    threading.Thread(target=watch, daemon=True, name="localflow-quit-watch").start()


def cmd_run(args, cfg) -> int:
    """The real thing: tray icon, global hotkeys, and the phone server."""
    if not _claim_single_instance():
        logging.getLogger("localflow").warning(
            "another LocalFlow tray app is already running in this session; exiting"
        )
        return 0
    from .platform import tk_first

    tk_first()  # a Mac: Tk before pystray touches AppKit (see platform.tk_first)
    from .engine import Engine
    from .hotkey import VK_CAPITAL, HotkeyManager, caps_lock_off
    from .tray import Tray

    engine = Engine(cfg)
    hotkeys = HotkeyManager()
    stopping = threading.Event()
    stopping_hooks: list = []  # also called on Quit (the Mac: end Tk's loop)

    def on_quit() -> None:
        stopping.set()
        for hook in stopping_hooks:
            hook()
    for binding in build_bindings(cfg, engine):
        hotkeys.bind(binding)

    def caps_is_a_hotkey() -> bool:
        return any(VK_CAPITAL in b.triggers for b in hotkeys._bindings)

    def rebind() -> None:
        hotkeys.replace(build_bindings(cfg, engine))
        if caps_is_a_hotkey():
            caps_lock_off()

    from .sounds import SoundCues

    cues = SoundCues(engine, cfg.sounds)
    from .always_on import Listener

    listener = Listener(engine, cfg)
    from .camera import Watcher
    from .health import OllamaHealth

    health = OllamaHealth(engine.cleaner)
    camera = Watcher(cfg.camera)
    listener.camera = camera
    tray = Tray(
        engine, on_quit=on_quit, sounds=cues, on_keys_changed=rebind, always_on=listener,
        health=health, camera=camera,
    )

    def ollama_changed(status: str, msg: str) -> None:
        engine.notice(msg, "ok" if status == "ok" else "error", 10)
        tray.notify(msg)
        tray.icon.update_menu()

    health.on_change = ollama_changed
    from . import commands as commands_mod

    commands_mod.camera_switch = tray.set_camera  # "camera on" / "camera off"
    tray.watch_settings()

    if cfg.overlay.enabled:
        from .overlay import Overlay

        live = None
        if cfg.overlay.live_text:
            from .live import LivePreview

            live = LivePreview(engine, cfg.overlay)
            listener.watch_live(live)
        overlay = Overlay(engine, cfg.overlay, live=live)
        overlay.camera = camera
        overlay.start()
    else:
        overlay = None

    def boot() -> None:
        # Before the models load (that can take a minute on some PCs): say
        # straight away whether an update just happened.
        outcome = _update_outcome()
        if outcome:
            kind, msg = outcome
            time.sleep(2.0)  # the overlay's window comes up first
            engine.notice(msg, kind, 12)
            tray.notify(msg + (" - see update.log" if kind == "error" else ""))
        engine.warmup()
        health.start()
        if cfg.camera.enabled:
            camera.start()
        if cfg.cleanup.method != "rules":
            health.suspect()  # check now, not in two minutes
        hotkeys.start()
        # Caps Lock left on before LocalFlow started can't be turned off by
        # hand while Caps Lock is a dictation key (the key is swallowed).
        if caps_is_a_hotkey():
            caps_lock_off()
        hint = _hint(cfg)
        updated = _version_change()
        if updated and not outcome:
            hint = f"Updated to {APP_NAME} {updated}. {hint}"
        if cfg.always_on.enabled and listener.ready:
            try:
                listener.start()
                hint += f'. Always-on: say "{listener.detector.phrases.start.text}"'
            except Exception as exc:
                logging.getLogger("localflow.always_on").error("always-on failed: %s", exc)
        tray.notify(hint, f"{APP_NAME} ready")

    threading.Thread(target=boot, daemon=True, name="localflow-boot").start()

    if cfg.server.enabled:
        from .server import serve

        threading.Thread(
            target=serve, args=(engine,), daemon=True, name="localflow-server"
        ).start()
        print(f"phone endpoint: http://localhost:{cfg.server.port}")

    _watch_quit_file(tray)
    try:
        if IS_MAC and overlay is not None:
            # Tk and the menu-bar icon both need the Mac's main thread: the
            # icon joins Tk's loop, which runs until Quit.
            stopping_hooks.append(overlay.quit)
            tray.run(detached=True)
            overlay.run_here()
        else:
            tray.run()  # blocks on the main thread until Quit
    except KeyboardInterrupt:
        pass
    finally:
        log = logging.getLogger("localflow")
        log.info("shutting down: listener")
        listener.stop()
        log.info("shutting down: hotkeys")
        hotkeys.stop()
        log.info("shutting down: models")
        engine.shutdown()
        log.info("shut down")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Shared so that both "localflow -v run" and "localflow run -v" work; argparse
    # otherwise only accepts global flags ahead of the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", type=Path, default=None, help="path to config.toml")
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="also write logs here (defaults to localflow.log for `run`)",
    )

    parser = argparse.ArgumentParser(prog="localflow", description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="tray app with global hotkeys (default)", parents=[common])
    sub.add_parser("check", help="verify GPU, mic, Ollama, hotkeys", parents=[common])
    sub.add_parser("devices", help="list audio input devices", parents=[common])
    sub.add_parser("level", help="live microphone level meter", parents=[common])

    p_start = sub.add_parser(
        "install-startup", help="run the tray app at login", parents=[common]
    )
    p_start.add_argument("--remove", action="store_true", help="uninstall instead")
    sub.add_parser("serve", help="run only the phone server", parents=[common])

    p_rec = sub.add_parser("record", help="record N seconds and transcribe", parents=[common])
    p_rec.add_argument("-s", "--seconds", type=float, default=5.0)
    p_rec.add_argument("--raw", action="store_true", help="skip cleanup")
    p_rec.add_argument("--rules", action="store_true", help="algorithmic cleanup, no LLM")

    p_tr = sub.add_parser("transcribe", help="transcribe an audio file", parents=[common])
    p_tr.add_argument("path")
    p_tr.add_argument("--raw", action="store_true", help="skip cleanup")
    p_tr.add_argument("--rules", action="store_true", help="algorithmic cleanup, no LLM")
    p_tr.add_argument(
        "--boost", choices=["none", "low", "medium", "high", "auto"], default="",
        help="apply a mic boost to the file first",
    )

    p_fm = sub.add_parser(
        "fetch-model", help="download the Whisper model (or zip it for another PC)", parents=[common]
    )
    p_fm.add_argument("--model", default="", help="model name (default: whisper.model)")
    p_fm.add_argument("--zip", default="", help="write a zip here for an offline install")
    p_fm.add_argument(
        "--fallback-model", default="", help="if the model is blocked, use this one instead"
    )

    p_im = sub.add_parser(
        "import-model", help="install a model from a zip or folder (offline)", parents=[common]
    )
    p_im.add_argument("path")
    p_im.add_argument("--name", default="", help="model name (default: from the folder)")

    sub.add_parser("enroll", help="set up always-on start/stop phrases", parents=[common])
    sub.add_parser("history", help="search past dictations; paste or copy one", parents=[common])
    sub.add_parser("settings", help="the Settings window", parents=[common])
    sub.add_parser("camera-calibrate", help="camera mode: look at each screen in turn",
                   parents=[common])
    p_li = sub.add_parser(
        "listen", help="try always-on mode in the console (pastes nothing)", parents=[common]
    )
    p_li.add_argument("--file", default="", help="feed a recording instead of the mic")
    p_li.add_argument("--quiet", action="store_true", help="don't print everything heard")
    p_li.add_argument("--rules", action="store_true", help="algorithmic cleanup, no LLM")

    p_cl = sub.add_parser("clean", help="clean a line of text (no audio)", parents=[common])
    p_cl.add_argument("text")
    p_cl.add_argument("--llm", action="store_true", help="use the LLM instead of the rules")

    args = parser.parse_args(argv)

    log_file = args.log_file
    if log_file is None and (args.command or "run") == "run":
        log_file = config_mod.PROJECT_ROOT / "localflow.log"
        _detach_hidden_console()
    _setup_logging(args.verbose, log_file)

    cfg, warnings = config_mod.load(args.config)
    for w in warnings:
        logging.getLogger("localflow.config").warning(w)
    # Before anything imports huggingface_hub, which reads it once at import.
    from .models import apply_endpoint

    apply_endpoint(cfg.whisper.hf_endpoint)

    handlers = {
        "run": cmd_run,
        "check": cmd_check,
        "clean": cmd_clean,
        "devices": cmd_devices,
        "enroll": cmd_enroll,
        "history": cmd_history,
        "settings": cmd_settings,
        "camera-calibrate": cmd_camera_calibrate,
        "listen": cmd_listen,
        "fetch-model": cmd_fetch_model,
        "import-model": cmd_import_model,
        "level": cmd_level,
        "install-startup": cmd_install_startup,
        "serve": cmd_serve,
        "record": cmd_record,
        "transcribe": cmd_transcribe,
    }
    from .models import ModelUnavailable

    try:
        return handlers[args.command or "run"](args, cfg)
    except ModelUnavailable as exc:
        print(f"! {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
