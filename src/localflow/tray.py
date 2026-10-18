"""System tray icon. The only UI the desktop app has."""

from __future__ import annotations

import logging
import threading
import time

from PIL import Image

from .engine import Engine, State

log = logging.getLogger(__name__)

#: The model tests/cleanup_quality.py was tuned against. Others work, but may
#: answer dictated questions instead of transcribing them - run that suite.
TESTED_MODEL = "qwen2.5:7b"
#: 12 GB card minus Whisper (~1.6 GB) and the desktop. Bigger models spill into
#: system RAM and cleanup goes from ~1 s to tens of seconds.
GPU_MODEL_BUDGET = 8.5e9
#: Measured best for character/tone: clearly stronger voices than qwen2.5:7b,
#: ~1.3 s warm for cleanup + style in one pass.
STYLE_MODEL = "gemma4:12B"

STATE_COLOURS = {
    State.IDLE: (110, 118, 129),
    State.LOADING: (188, 140, 40),
    State.RECORDING: (219, 68, 68),
    State.TRANSCRIBING: (56, 139, 232),
    State.CLEANING: (137, 87, 229),
    State.INJECTING: (46, 160, 90),
    State.COMMAND: (46, 196, 182),
    State.ERROR: (219, 68, 68),
}


QUIT_GRACE = 8.0

#: What the Settings window can change while LocalFlow runs: {section: keys}.
#: Anything else in config.toml still needs a restart.
LIVE_SETTINGS = {
    "cleanup": ("enabled", "method", "model"),
    "style": ("character", "tone", "model"),
    "inject": ("paste_to",),
    "hotkeys": ("dictate", "dictate_raw"),
    "audio": ("boost",),
    "sounds": ("enabled", "start", "stop", "paste", "response", "volume"),
    "always_on": ("default_mode", "command_mode_seconds", "idle_stop_seconds", "command_llm",
                  "cancel_words", "exit_words", "quick", "quick_stop", "quick_send",
                  "code_words"),
    "overlay": ("theme", "opacity", "label_opacity", "label_theme", "label_size"),
    "history": ("enabled",),
}


def _exit_if_stuck() -> None:
    """Quit means quit. If the clean shutdown hasn't finished in QUIT_GRACE
    seconds, write every thread's stack to crash.log (so the cause shows next
    time) and end the process. A copy on a work PC kept its icon after Quit,
    stopped answering, then carried on as if nothing had happened."""

    def watchdog() -> None:
        time.sleep(QUIT_GRACE)
        import faulthandler
        import os

        log.warning("quit took over %.0fs - forcing it (stacks in crash.log)", QUIT_GRACE)
        try:
            from .config import PROJECT_ROOT

            with open(PROJECT_ROOT / "crash.log", "a", encoding="utf-8") as fh:
                fh.write(f"--- quit stuck {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                fh.flush()
                faulthandler.dump_traceback(fh, all_threads=True)
        except Exception:
            pass
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        os._exit(0)

    threading.Thread(target=watchdog, daemon=True, name="localflow-quit-watchdog").start()


def _update_step(log_file, start: int) -> str:
    """The installer's current step ("Python and dependencies") from the part
    of update.log written by this run."""
    import re

    try:
        with open(log_file, "rb") as fh:
            fh.seek(start)
            text = fh.read().decode("ascii", "replace")
    except OSError:
        return ""
    steps = re.findall(r"== ([^\r\n]+)", text)
    if not steps:
        return "fetching" if "update started" in text else ""
    step = steps[-1].strip()
    return re.sub(r"\s*\(.*?\)$", "", step)[:28]


def open_history() -> None:
    """The history window, as its own process (it has a Tk loop of its own)."""
    from .proc import run_localflow

    threading.Thread(target=run_localflow, args=("history",), daemon=True,
                     name="localflow-history").start()


def _icon(colour: tuple[int, int, int], ring: bool = False) -> Image.Image:
    """The golden wind, with a dot for the state (none when idle)."""
    from .icon import golden_wind

    idle = colour == STATE_COLOURS[State.IDLE]
    return golden_wind(None if idle else colour, ring)


class Tray:
    def __init__(
        self, engine: Engine, on_quit=None, sounds=None, on_keys_changed=None, always_on=None,
        health=None,
    ):
        import pystray

        self.engine = engine
        self.always_on = always_on  # always_on.Listener, or None
        self.health = health  # health.OllamaHealth, or None
        self._setup_open = False
        self._settings_open = False
        self._updating = False
        self._on_quit = on_quit
        self._on_keys_changed = on_keys_changed
        self.sounds = sounds  # SoundCues, or None when cues are not wired up
        self._last_text = ""
        self._recording_since: float | None = None
        self._ticker: threading.Thread | None = None
        from .updates import Checker

        self.updates = Checker(on_new=self._new_version)

        from .icon import set_app_identity

        set_app_identity()
        self.icon = pystray.Icon(
            "localflow",
            icon=_icon(STATE_COLOURS[State.IDLE]),
            title="LocalFlow - starting",
            menu=pystray.Menu(
                pystray.MenuItem("Copy last transcript", self._copy_last),
                pystray.MenuItem("Dictation history...", lambda icon, item: open_history()),
                pystray.MenuItem("Settings...", lambda icon, item: self.open_settings()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Always-on listening",
                    self._toggle_always_on,
                    checked=lambda _: bool(self.always_on and self.always_on.running),
                    visible=lambda _: self.always_on is not None,
                ),
                pystray.MenuItem(
                    "Set up always-on phrases...",
                    lambda icon, item: self._open_setup(),
                    visible=lambda _: self.always_on is not None,
                ),
                pystray.MenuItem(
                    "Command mode",
                    self._toggle_commands,
                    checked=lambda _: bool(self.always_on and self.always_on.commanding),
                    visible=lambda _: bool(self.always_on and self.always_on.running),
                ),
                pystray.MenuItem(
                    "Quick mode",
                    self._toggle_quick,
                    checked=lambda _: self.engine.cfg.always_on.quick,
                    visible=lambda _: bool(self.always_on and self.always_on.running),
                ),
                pystray.MenuItem(
                    lambda _: self._near_label(),
                    lambda icon, item: self._accept_near(),
                    visible=lambda _: bool(self.always_on and self.always_on.last_near),
                ),
                pystray.MenuItem(
                    lambda _: f"Microphone: {self._mic_label()}",
                    pystray.Menu(lambda: self._mic_items(pystray)),
                ),
                pystray.MenuItem(
                    "Turn Caps Lock off",
                    lambda icon, item: self._caps_off(),
                    visible=lambda _: self._caps_on(),
                ),
                # Only when Ollama is in trouble: the rest of the time it's in Settings.
                pystray.MenuItem(
                    lambda _: f"Restart Ollama ({self.health.label()[8:]})" if self.health else "",
                    lambda icon, item: self._restart_ollama(),
                    visible=lambda _: self._llm_in_use() and not self.health.ok,
                    enabled=lambda _: not (self.health and self.health.restarting),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(lambda _: self.updates.label(), None, enabled=False),
                pystray.MenuItem(
                    lambda _: self._update_label(),
                    lambda icon, item: self._update(),
                    enabled=lambda _: not self._updating,
                ),
                pystray.MenuItem(lambda _: self._status_line(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            ),
        )

        engine.on_state(self._on_state)
        engine.on_result(self._on_result)

    # -- always-on ---------------------------------------------------------

    def _save_always_on(self, on: bool) -> None:
        self.engine.cfg.always_on.enabled = on
        try:
            from .config import save_setting

            save_setting("always_on", "enabled", on)
        except OSError as exc:
            log.warning("could not save always-on: %s", exc)

    def _toggle_always_on(self, icon, item) -> None:
        lis = self.always_on
        if lis is None:
            return
        if lis.running:
            lis.stop()
            self._save_always_on(False)
            self.notify("Always-on listening is off")
        elif not lis.ready:
            self._open_setup()
        else:
            try:
                lis.start()
            except Exception as exc:
                self.notify(f"Always-on could not start: {exc}")
                return
            self._save_always_on(True)
            self.notify(f'Listening. Say "{lis.detector.phrases.start.text}" to dictate, '
                        f'"{lis.detector.phrases.stop.text}" to finish.')
        icon.update_menu()

    def _open_setup(self) -> None:
        """Run the setup window as its own process; reload when it saves."""
        if self._setup_open or self.always_on is None:
            return
        from .proc import run_localflow

        lis = self.always_on
        self._setup_open = True
        lis.paused = True  # don't trigger on the takes being recorded

        def run() -> None:
            try:
                code = run_localflow("enroll")
                if code == 0:
                    lis.reload()
                    if not lis.running:
                        lis.start()
                    self._save_always_on(True)
                    self.notify(
                        f'Always-on is listening. Say "{lis.detector.phrases.start.text}" to '
                        f'dictate, "{lis.detector.phrases.stop.text}" to finish.'
                    )
            except Exception as exc:
                log.exception("always-on setup failed")
                self.notify(f"Always-on setup failed: {exc}")
            finally:
                lis.paused = False
                self._setup_open = False
                self.icon.update_menu()

        threading.Thread(target=run, daemon=True, name="localflow-setup").start()

    def _toggle_commands(self, icon, item) -> None:
        lis = self.always_on
        if lis is None:
            return
        if lis.commanding:
            lis.leave_commands()
        else:
            lis.enter_commands()
        icon.update_menu()

    def _toggle_quick(self, icon, item) -> None:
        """Quick mode on or off: the end words and the code words."""
        from .config import save_setting

        on = self.engine.cfg.always_on
        on.quick = not on.quick
        try:
            save_setting("always_on", "quick", on.quick)
        except OSError as exc:
            log.warning("could not save quick mode: %s", exc)
        self._quick_changed()
        icon.update_menu()

    def _quick_changed(self) -> None:
        on = self.engine.cfg.always_on
        if self.always_on is not None:
            self.always_on.grammar.set_code_words(on.code_words if on.quick else [])
        if on.quick:
            stop, send = (on.quick_stop or ["?"])[0], (on.quick_send or ["?"])[0]
            self.engine.notice(f'Quick mode: end with "{stop}", or "{send}" to press Enter',
                               "info", 5)
        else:
            self.engine.notice("Quick mode off", "info", 3)

    def _near_label(self) -> str:
        near = self.always_on.last_near if self.always_on else None
        if not near:
            return ""
        which, heard = near
        return f'Accept "{heard}" as the {which} phrase'

    def _accept_near(self) -> None:
        """A phrase that was heard but not accepted: take it as a spelling."""
        lis = self.always_on
        near = lis.last_near if lis else None
        if not near:
            return
        which, heard = near
        from .wake import Phrases

        phrases = Phrases.load()
        phrase = getattr(phrases, which)
        if heard not in phrase.variants:
            phrase.variants.append(heard)
            phrases.save()
            lis.reload()
        lis.last_near = None
        log.info("always-on: added a %s phrase spelling", which)
        self.notify(f'"{heard}" now counts as your {which} phrase')
        self.icon.update_menu()

    # -- learned phrases ----------------------------------------------------

    # -- microphone ---------------------------------------------------------

    def _mic_label(self) -> str:
        name = self.engine.cfg.audio.device
        return (name[:28] + "…" if len(name) > 29 else name) if name else "system default"

    def _mic_items(self, pystray) -> list:
        from .audio import microphones

        try:
            names = microphones()
        except Exception as exc:
            log.warning("could not list microphones: %s", exc)
            names = []
        current = self.engine.cfg.audio.device

        def pick(name: str):
            def action(icon, item) -> None:
                self._set_mic(name)
            return action

        items = [pystray.MenuItem("System default (follows Windows)", pick(""),
                                  checked=lambda _: not self.engine.cfg.audio.device, radio=True)]
        items += [pystray.MenuItem(n, pick(n), checked=lambda _, n=n: current == n, radio=True)
                  for n in names]
        items += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem("Refresh the list", lambda icon, item: self._refresh_mics())]
        return items

    def _set_mic(self, name: str) -> None:
        self.engine.cfg.audio.device = name
        try:
            from .config import save_setting

            save_setting("audio", "device", name)
        except OSError as exc:
            log.warning("could not save the microphone: %s", exc)
        lis = self.always_on
        if lis is not None and lis.running:
            threading.Thread(target=lis.reopen, args=("microphone picked in the tray",),
                             daemon=True, name="localflow-mic").start()
        else:
            self.engine.notice(f"Microphone: {name or 'system default'}", "info", 5)
        self.icon.update_menu()

    def _refresh_mics(self) -> None:
        """Re-read the devices (PortAudio keeps the list from when it started)."""
        lis = self.always_on
        if lis is not None and lis.running:
            threading.Thread(target=lis.reopen, args=("refreshing the microphone list",),
                             daemon=True, name="localflow-mic").start()
        elif not self.engine.recording:
            from .audio import refresh_devices

            refresh_devices()
        self.icon.update_menu()

    # -- update ------------------------------------------------------------

    def _update_label(self) -> str:
        from .updates import current, install_incomplete

        if self._updating:
            return "Updating..."
        if install_incomplete():
            return f"Finish installing {current()}"
        if self.updates.update_available:
            return f"Update to {self.updates.latest}"
        return "Check for updates"

    def _new_version(self, latest: str) -> None:
        from .updates import current

        self.engine.notice(f"LocalFlow {latest} is out - tray > Update", "info", 10)
        self.notify(f"LocalFlow {latest} is available (you have {current()}). "
                    "Right-click the tray icon > Update to get it.")
        self.icon.update_menu()

    def _update(self) -> None:
        """Run update.ps1 detached; it restarts us if there was an update.
        Every step is said both in the pill and as a toast (Windows can hide
        toasts), so it's never a guess whether it worked."""
        from .config import PROJECT_ROOT
        from .proc import launch_detached_powershell
        from .updates import current

        result = PROJECT_ROOT / "update.result"
        result.unlink(missing_ok=True)
        self._updating = True
        self.icon.update_menu()
        self.engine.notice("Checking for updates…", "info", 30)

        def watch() -> None:
            from .updates import install_incomplete

            latest = self.updates.check()
            if self.updates.update_available or install_incomplete():
                target = latest if self.updates.update_available else current()
                self.engine.notice(f"Updating to {target}…", "info", 600)
                self.notify(f"Updating to LocalFlow {target}. It restarts when it's done; "
                            "the tray icon may disappear for a minute.")
            log_file = PROJECT_ROOT / "update.log"
            start_size = log_file.stat().st_size if log_file.exists() else 0
            launch_detached_powershell(PROJECT_ROOT / "update.ps1")
            self.updates.latest = latest or self.updates.latest
            deadline = time.monotonic() + 30 * 60
            last_change, last_size, step = time.monotonic(), start_size, ""
            while time.monotonic() < deadline:
                time.sleep(2)
                try:
                    code, _, msg = result.read_text(encoding="ascii").strip().partition(" ")
                except OSError:
                    # Still running: show its current step, and notice if it stalls.
                    size = log_file.stat().st_size if log_file.exists() else 0
                    if size != last_size:
                        last_change, last_size = time.monotonic(), size
                        now_step = _update_step(log_file, start_size)
                        if now_step and now_step != step:
                            step = now_step
                            self.engine.notice(f"Updating: {step}…", "info", 600)
                    elif time.monotonic() - last_change > 5 * 60:
                        where = step or "starting"
                        self.engine.notice(f"Update stuck? ({where})", "error", 30)
                        self.notify(f"The update hasn't moved for 5 minutes (at: {where}). "
                                    "It may still finish; update.log in the LocalFlow folder "
                                    "shows what it's doing. You can try Update again.")
                        break
                    continue
                if code == "3":
                    self.engine.notice(f"Up to date: LocalFlow {current()}", "ok", 8)
                    self.notify(f"You're on the latest version, LocalFlow {current()}.")
                elif code == "1":
                    self.engine.notice("Update failed - see update.log", "error", 12)
                    self.notify(f"Update failed: {msg} (details in update.log)")
                # "0": the updater restarts LocalFlow; the new one says so.
                break
            self._updating = False
            self.icon.update_menu()

        threading.Thread(target=watch, daemon=True, name="localflow-update").start()

    # -- dictation keys ----------------------------------------------------

    @staticmethod
    def _caps_on() -> bool:
        try:
            from .hotkey import caps_lock_on

            return caps_lock_on()
        except Exception:
            return False

    def _caps_off(self) -> None:
        from .hotkey import caps_lock_off

        caps_lock_off()

    # -- settings ------------------------------------------------------------

    def open_settings(self) -> None:
        """The Settings window, as its own process (one at a time)."""
        if self._settings_open:
            return
        from .proc import run_localflow

        self._settings_open = True

        def run() -> None:
            try:
                run_localflow("settings")
            finally:
                self._settings_open = False

        threading.Thread(target=run, daemon=True, name="localflow-settings").start()

    def watch_settings(self) -> None:
        """Apply changes saved to config.toml (by Settings, or by hand) and
        to learned.json, within a second."""
        from .commands import LEARNED
        from .config import CONFIG_PATH

        def mtime(path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        def loop() -> None:
            seen = {CONFIG_PATH: mtime(CONFIG_PATH), LEARNED: mtime(LEARNED)}
            while True:
                time.sleep(1.0)
                for path in seen:
                    now = mtime(path)
                    if now == seen[path]:
                        continue
                    seen[path] = now
                    try:
                        if path == CONFIG_PATH:
                            from .config import load

                            self.apply_settings(load()[0])
                        elif self.always_on is not None:
                            from .commands import load_learned

                            self.always_on.grammar.learned = load_learned(LEARNED)
                    except Exception:
                        log.exception("applying %s failed", path.name)

        threading.Thread(target=loop, daemon=True, name="localflow-settings-watch").start()

    def apply_settings(self, new) -> dict:
        """Copy what Settings can change from a freshly loaded config into the
        live one, and do what each change needs. Returns {key: (old, new)}."""
        cur = self.engine.cfg
        changed = {}
        for section, keys in LIVE_SETTINGS.items():
            a, b = getattr(cur, section), getattr(new, section)
            for key in keys:
                old, value = getattr(a, key), getattr(b, key)
                if old != value:
                    setattr(a, key, value)
                    changed[f"{section}.{key}"] = (old, value)
        if not changed:
            return changed
        log.info("settings changed: %s", ", ".join(sorted(changed)))
        cleaner = self.engine.cleaner
        if "cleanup.method" in changed or "cleanup.model" in changed:
            if cur.cleanup.method == "rules":
                # Hand the cleanup model's VRAM back; nothing will use it now.
                threading.Thread(target=cleaner.unload, daemon=True,
                                 name="localflow-model-unload").start()
            else:
                previous = changed.get("cleanup.model", (cur.cleanup.model,))[0]

                def switch() -> None:
                    cleaner.switch_model(cur.cleanup.model, previous)
                    if self.health is not None:
                        self.health.suspect()  # is the new one answering?

                threading.Thread(target=switch, daemon=True, name="localflow-model-switch").start()
        if "style.model" in changed and cur.style.model and self.engine.stylizer.active:
            threading.Thread(target=cleaner.preload, args=(cur.style.model,), daemon=True).start()
        if ("hotkeys.dictate" in changed or "hotkeys.dictate_raw" in changed) and self._on_keys_changed:
            self._on_keys_changed()
        lis = self.always_on
        if lis is not None:
            if "always_on.default_mode" in changed:
                lis.set_default_mode(cur.always_on.default_mode)
            if {"always_on.command_mode_seconds", "always_on.idle_stop_seconds"} & changed.keys():
                lis._extend_mode()
            if {"always_on.cancel_words", "always_on.exit_words"} & changed.keys():
                lis.grammar.set_escape_words(cur.always_on.cancel_words, cur.always_on.exit_words)
            if "always_on.quick" in changed:
                self._quick_changed()
            elif "always_on.code_words" in changed:
                lis.grammar.set_code_words(
                    cur.always_on.code_words if cur.always_on.quick else [])
        if "overlay.theme" in changed:
            from .themes import resolve

            self.engine.notice(f"Theme: {resolve(cur.overlay.theme).name}", "ok", 3)
        try:
            self.icon.update_menu()
        except Exception:
            pass
        return changed

    def _llm_in_use(self) -> bool:
        return self.health is not None and self.engine.cfg.cleanup.method != "rules"

    def _restart_ollama(self) -> None:
        health = self.health
        if health is None or health.restarting:
            return
        self.notify("Restarting Ollama...")

        def go() -> None:
            health.restart()
            if health.ok:
                self.notify(f"Ollama is back ({self.engine.cfg.cleanup.model} loaded)")
            else:
                self.notify(f"Ollama restarted but is still {health.label()[8:]} - see the log")
            self.icon.update_menu()

        threading.Thread(target=go, daemon=True, name="localflow-ollama-restart").start()

    def _status_line(self) -> str:
        h = self.engine.history
        if not h:
            return "no dictations yet"
        last = h[-1]
        return f"last: {last.audio_seconds:.1f}s -> {last.total_seconds:.1f}s ({last.speedup:.0f}x)"

    def _on_state(self, state: State, detail: str) -> None:
        colour = STATE_COLOURS.get(state, STATE_COLOURS[State.IDLE])
        try:
            self.icon.icon = _icon(colour, ring=(state == State.RECORDING))
            label = state.value if not detail else f"{state.value} - {detail}"
            self.icon.title = f"LocalFlow: {label}"[:127]
        except Exception:
            log.debug("tray update failed", exc_info=True)

        if state == State.RECORDING:
            self._start_ticker()
        else:
            self._recording_since = None

    def _start_ticker(self) -> None:
        """Show elapsed time while recording.

        In toggle mode there is no held key to remind you a recording is live,
        so the tooltip has to carry that information.
        """
        self._recording_since = time.monotonic()
        if self._ticker and self._ticker.is_alive():
            return

        def tick() -> None:
            while True:
                started = self._recording_since
                if started is None:
                    return
                elapsed = int(time.monotonic() - started)
                try:
                    self.icon.title = f"LocalFlow: recording {elapsed // 60}:{elapsed % 60:02d}"
                except Exception:
                    return
                time.sleep(1.0)

        self._ticker = threading.Thread(target=tick, daemon=True, name="localflow-tray-tick")
        self._ticker.start()

    def _on_result(self, result) -> None:
        self._last_text = result.text

    def _copy_last(self) -> None:
        if not self._last_text:
            return
        from .inject import set_clipboard_text

        try:
            set_clipboard_text(self._last_text)
        except Exception as exc:
            log.warning("copy failed: %s", exc)

    def _quit(self) -> None:
        log.info("quitting")
        _exit_if_stuck()
        if self._on_quit:
            self._on_quit()
        self.icon.stop()

    def stop(self) -> None:
        """Quit as if from the menu (tray icon removed properly). Any thread."""
        self._quit()

    def notify(self, message: str, title: str = "") -> None:
        from . import APP_NAME

        title = title or APP_NAME
        try:
            self.icon.notify(message, title)
        except Exception:
            log.debug("notification failed", exc_info=True)

    def run(self) -> None:
        """Blocking; must be called from the main thread on Windows."""
        threading.Thread(target=self._watch_caps, daemon=True, name="localflow-caps").start()
        self.updates.start()
        self.icon.run()

    def _watch_caps(self) -> None:
        """pystray builds the menu ahead of time, not on right-click, so the
        "Turn Caps Lock off" entry only appears if the menu is rebuilt when
        Caps Lock changes. One GetKeyState per second costs nothing."""
        last = None
        while True:
            now = self._caps_on()
            if now != last:
                last = now
                try:
                    self.icon.update_menu()
                except Exception:
                    pass
            time.sleep(1.0)

    def run_detached(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="localflow-tray")
        t.start()
        return t
