"""System tray icon. The only UI the desktop app has."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

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
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if ring:
        d.ellipse((2, 2, size - 3, size - 3), outline=colour + (255,), width=5)
    else:
        d.ellipse((10, 10, size - 11, size - 11), fill=colour + (255,))
    # A stubby mic silhouette so the icon reads as dictation at 16px.
    d.rounded_rectangle((26, 18, 38, 38), radius=6, fill=(255, 255, 255, 235))
    d.arc((20, 28, 44, 46), start=0, end=180, fill=(255, 255, 255, 235), width=4)
    d.line((32, 44, 32, 50), fill=(255, 255, 255, 235), width=4)
    return img


class Tray:
    def __init__(
        self, engine: Engine, on_quit=None, sounds=None, on_keys_changed=None, always_on=None
    ):
        import pystray

        self.engine = engine
        self.always_on = always_on  # always_on.Listener, or None
        self._setup_open = False
        self._updating = False
        self._on_quit = on_quit
        self._on_keys_changed = on_keys_changed
        self.sounds = sounds  # SoundCues, or None when cues are not wired up
        self._last_text = ""
        self._recording_since: float | None = None
        self._ticker: threading.Thread | None = None
        from .updates import Checker

        self.updates = Checker(on_new=self._new_version)

        self.icon = pystray.Icon(
            "localflow",
            icon=_icon(STATE_COLOURS[State.IDLE]),
            title="LocalFlow - starting",
            menu=pystray.Menu(
                pystray.MenuItem("Copy last transcript", self._copy_last),
                pystray.MenuItem("Dictation history...", lambda icon, item: open_history()),
                pystray.MenuItem(
                    "Cleanup",
                    self._toggle_cleanup,
                    checked=lambda _: self.engine.cfg.cleanup.enabled,
                ),
                pystray.MenuItem(
                    lambda _: self._cleanup_label(),
                    pystray.Menu(lambda: self._model_items(pystray)),
                ),
                pystray.MenuItem(
                    lambda _: f"Character: {self.engine.cfg.style.character}",
                    self._character_menu(pystray),
                ),
                pystray.MenuItem(
                    lambda _: f"Tone: {self.engine.cfg.style.tone}", self._tone_menu(pystray)
                ),
                pystray.MenuItem(
                    lambda _: f"Style model: {self.engine.cfg.style.model or 'same as cleanup'}",
                    pystray.Menu(lambda: self._style_model_items(pystray)),
                ),
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
                    lambda _: self._near_label(),
                    lambda icon, item: self._accept_near(),
                    visible=lambda _: bool(self.always_on and self.always_on.last_near),
                ),
                pystray.MenuItem(
                    "Learned phrases",
                    pystray.Menu(lambda: self._learned_items(pystray)),
                    visible=lambda _: self.always_on is not None,
                ),
                pystray.MenuItem(
                    "Paste where I started",
                    self._toggle_paste_to,
                    checked=lambda _: self.engine.cfg.inject.paste_to == "start",
                ),
                pystray.MenuItem(lambda _: self._keys_label(), self._keys_menu(pystray)),
                pystray.MenuItem(
                    "Turn Caps Lock off",
                    lambda icon, item: self._caps_off(),
                    visible=lambda _: self._caps_on(),
                ),
                pystray.MenuItem(
                    lambda _: f"Microphone: {self._mic_label()}",
                    pystray.Menu(lambda: self._mic_items(pystray)),
                ),
                pystray.MenuItem(
                    lambda _: f"Mic boost: {self.engine.recorder.boost.capitalize()}",
                    self._boost_menu(pystray),
                ),
                pystray.MenuItem("Start sound", self._sound_menu(pystray, "start")),
                pystray.MenuItem("Stop sound", self._sound_menu(pystray, "stop")),
                pystray.MenuItem("Paste sound", self._sound_menu(pystray, "paste")),
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

    def _sound_menu(self, pystray, which: str):
        """Radio list of cues. Picking one previews it and saves it to config.toml."""
        from .sounds import CHOICES

        cfg = self.engine.cfg.sounds

        def pick(choice: str):
            def action(icon, item) -> None:
                setattr(cfg, which, choice)
                self._preview(choice)
                try:
                    from .config import save_setting

                    save_setting("sounds", which, choice)
                except OSError as exc:
                    log.warning("could not save sound choice: %s", exc)

            return action

        items = [
            pystray.MenuItem(
                name.capitalize(),
                pick(name),
                checked=lambda item, name=name: getattr(cfg, which).lower() == name,
                radio=True,
            )
            for name in CHOICES
        ]
        # A custom .wav set in config.toml shows up as its own checked entry.
        is_custom = lambda item: getattr(cfg, which).lower() not in CHOICES  # noqa: E731
        items.append(
            pystray.MenuItem(
                lambda item: f"Custom: {Path(getattr(cfg, which)).name}",
                lambda icon, item: self._preview(getattr(cfg, which)),
                checked=is_custom,
                radio=True,
                visible=is_custom,
            )
        )
        return pystray.Menu(*items)

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

    def _learned_items(self, pystray) -> list:
        """What's been learned from missed commands; click one to forget it."""
        from .commands import BY_NAME, Parsed, describe

        lis = self.always_on
        learned = lis.grammar.learned if lis is not None else {}
        if not learned:
            return [pystray.MenuItem('None yet: say "remember that" when offered', None,
                                     enabled=False)]

        def forget(phrase: str):
            def action(icon, item) -> None:
                lis.grammar.forget(phrase)
                self.notify(f'Forgot "{phrase}"')
                icon.update_menu()
            return action

        items = [pystray.MenuItem("Click one to forget it", None, enabled=False)]
        for phrase, d in sorted(learned.items()):
            p = Parsed(BY_NAME[d["command"]], d.get("arg", ""), 1.0, where=d.get("where", ""))
            items.append(pystray.MenuItem(f'"{phrase}" = {describe(p)}', forget(phrase)))
        return items

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

    def _toggle_paste_to(self, icon, item) -> None:
        cfg = self.engine.cfg.inject
        cfg.paste_to = "focus" if cfg.paste_to == "start" else "start"
        try:
            from .config import save_setting

            save_setting("inject", "paste_to", cfg.paste_to)
        except OSError as exc:
            log.warning("could not save paste target: %s", exc)
        self.notify(
            "Text goes to the window you were in when you started dictating"
            if cfg.paste_to == "start" else "Text goes to whichever window has focus when it's ready"
        )
        icon.update_menu()

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

    def _key_lists(self) -> tuple[list[str], list[str]]:
        h = self.engine.cfg.hotkeys
        as_list = lambda v: [v] if isinstance(v, str) and v else list(v or [])  # noqa: E731
        return as_list(h.dictate), as_list(h.dictate_raw)

    def _keys_label(self) -> str:
        from .hotkey import KEY_PRESETS

        dictate, _ = self._key_lists()
        if not dictate:
            return "Dictation keys: none"
        names = {spec: name for name, (spec, _raw) in KEY_PRESETS.items()}
        return "Dictation keys: " + ", ".join(names.get(s, s) for s in dictate)

    def _set_keys(self, dictate: list[str], raw: list[str]) -> None:
        h = self.engine.cfg.hotkeys
        h.dictate, h.dictate_raw = dictate, raw
        try:
            from .config import save_setting

            save_setting("hotkeys", "dictate", dictate)
            save_setting("hotkeys", "dictate_raw", raw)
        except OSError as exc:
            log.warning("could not save dictation keys: %s", exc)
        if self._on_keys_changed:
            self._on_keys_changed()
        self.icon.update_menu()
        names = self._keys_label().split(": ", 1)[1]
        self.notify(
            f"Dictation keys: {names}" if dictate else "No dictation key - pick one here any time",
            "LocalFlow",
        )

    def _keys_menu(self, pystray):
        """Checkboxes: any mix of the presets, or none. Takes effect at once."""
        from .hotkey import KEY_PRESETS

        def toggle(name: str):
            spec, raw_spec = KEY_PRESETS[name]

            def action(icon, item) -> None:
                dictate, raw = self._key_lists()
                if spec in dictate:
                    dictate = [s for s in dictate if s != spec]
                    raw = [s for s in raw if s != raw_spec]
                else:
                    dictate.append(spec)
                    if raw_spec not in raw:
                        raw.append(raw_spec)
                self._set_keys(dictate, raw)

            return action

        def labels(name: str) -> str:
            spec, raw_spec = KEY_PRESETS[name]
            raw = "F10" if raw_spec == "<f10>" else f"Shift+{name}"
            return f"{name}   (raw: {raw})"

        items = [
            pystray.MenuItem(
                labels(name),
                toggle(name),
                checked=lambda item, spec=spec: spec in self._key_lists()[0],
            )
            for name, (spec, _raw) in KEY_PRESETS.items()
        ]
        presets = {spec for spec, _raw in KEY_PRESETS.values()}
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "None (no dictation key)",
                lambda icon, item: self._set_keys([], []),
                checked=lambda item: not self._key_lists()[0],
                radio=True,
            ),
            # Keys set by hand in config.toml that aren't presets: shown, kept.
            pystray.MenuItem(
                lambda item: "Also from config.toml: "
                + ", ".join(s for s in self._key_lists()[0] if s not in presets),
                None,
                enabled=False,
                visible=lambda item: any(s not in presets for s in self._key_lists()[0]),
            ),
        ]
        return pystray.Menu(*items)

    def _boost_menu(self, pystray):
        """Radio list of mic boosts. Takes effect from the next recording."""
        cfg = self.engine.cfg.audio
        labels = {
            "none": "None",
            "low": "Low (+6 dB)",
            "medium": "Medium (+12 dB)",
            "high": "High (+18 dB)",
            "auto": "Auto (lifts quiet speech)",
        }

        def pick(choice: str):
            def action(icon, item) -> None:
                cfg.boost = choice
                try:
                    from .config import save_setting

                    save_setting("audio", "boost", choice)
                except OSError as exc:
                    log.warning("could not save mic boost: %s", exc)

            return action

        return pystray.Menu(
            *[
                pystray.MenuItem(
                    label,
                    pick(name),
                    checked=lambda item, name=name: self.engine.recorder.boost == name,
                    radio=True,
                )
                for name, label in labels.items()
            ]
        )

    def _style_action(self, key: str, value: str):
        """Set [style] key (character / tone) now and persist it."""

        def action(icon, item) -> None:
            setattr(self.engine.cfg.style, key, value)
            try:
                from .config import save_setting

                save_setting("style", key, value)
            except OSError as exc:
                log.warning("could not save style %s: %s", key, exc)
            icon.update_menu()

        return action

    def _character_menu(self, pystray):
        from .style import all_characters

        cfg = self.engine.cfg.style

        def items():
            names = ["none", *all_characters(cfg)]
            return [
                pystray.MenuItem(
                    name.title() if name != "none" else "None",
                    self._style_action("character", name),
                    checked=lambda item, name=name: cfg.character.lower() == name,
                    radio=True,
                )
                for name in names
            ]

        return pystray.Menu(items)

    def _tone_menu(self, pystray):
        """Neutral, then Plutchik's 8 emotions, each with its 3 intensities."""
        from .style import WHEEL

        cfg = self.engine.cfg.style

        def level_item(level):
            return pystray.MenuItem(
                f"{level.name.title()}  ({level.intensity})",
                self._style_action("tone", level.name),
                checked=lambda item: cfg.tone.lower() == level.name,
                radio=True,
            )

        items = [
            pystray.MenuItem(
                "Neutral",
                self._style_action("tone", "neutral"),
                checked=lambda item: cfg.tone.lower() == "neutral",
                radio=True,
            ),
            pystray.Menu.SEPARATOR,
        ]
        for emotion, levels in WHEEL.items():
            names = {lv.name for lv in levels}
            items.append(
                pystray.MenuItem(
                    emotion.title(),
                    pystray.Menu(*[level_item(lv) for lv in levels]),
                    checked=lambda item, names=names: cfg.tone.lower() in names,
                )
            )
        return pystray.Menu(*items)

    def _style_model_items(self, pystray) -> list:
        """Model for the character/tone pass. A model other than the cleanup one
        does cleanup + style in a single pass (see Stylizer.separate_model)."""
        cfg = self.engine.cfg

        def pick(name: str):
            def action(icon, item) -> None:
                cfg.style.model = name
                try:
                    from .config import save_setting

                    save_setting("style", "model", name)
                except OSError as exc:
                    log.warning("could not save style model: %s", exc)
                if name and self.engine.stylizer.active:
                    # Load it now so the next dictation does not wait ~30 s.
                    threading.Thread(
                        target=self.engine.cleaner.preload, args=(name,), daemon=True
                    ).start()
                icon.update_menu()

            return action

        items = [
            pystray.MenuItem(
                f"Same as cleanup ({cfg.cleanup.model})",
                pick(""),
                checked=lambda item: not cfg.style.model,
                radio=True,
            ),
            pystray.Menu.SEPARATOR,
        ]
        for name, size in sorted(self.engine.cleaner.list_models(), key=lambda m: m[1]):
            label = f"{name}  ({size / 1e9:.1f} GB)"
            if name == STYLE_MODEL:
                label += "  - stronger voices"
            elif size > GPU_MODEL_BUDGET:
                label += "  - too big for GPU, slow"
            items.append(
                pystray.MenuItem(
                    label,
                    pick(name),
                    checked=lambda item, name=name: cfg.style.model == name,
                    radio=True,
                )
            )
        return items

    def _cleanup_label(self) -> str:
        cfg = self.engine.cfg.cleanup
        if cfg.method == "rules":
            return "Cleanup: algorithmic (no LLM)"
        return f"Cleanup model: {cfg.model}"

    def _model_items(self, pystray) -> list:
        """Algorithmic cleanup plus the installed Ollama models, rebuilt each
        time the menu is refreshed."""
        cfg = self.engine.cfg.cleanup
        items = [
            pystray.MenuItem(
                "Algorithmic (no LLM, instant)",
                lambda icon, item: self._pick_rules(icon),
                checked=lambda item: cfg.method == "rules",
                radio=True,
            ),
            pystray.Menu.SEPARATOR,
        ]
        models = self.engine.cleaner.list_models()
        if not models:
            return items + [
                pystray.MenuItem("Ollama not reachable", None, enabled=False),
                pystray.MenuItem("Refresh list", lambda icon, item: icon.update_menu()),
            ]
        for name, size in sorted(models, key=lambda m: m[1]):
            label = f"{name}  ({size / 1e9:.1f} GB)"
            if name == TESTED_MODEL:
                label += "  - tested"
            elif size > GPU_MODEL_BUDGET:
                label += "  - too big for GPU, slow"
            items.append(
                pystray.MenuItem(
                    label,
                    self._model_action(name),
                    checked=lambda item, name=name: cfg.method != "rules" and cfg.model == name,
                    radio=True,
                )
            )
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Refresh list", lambda icon, item: icon.update_menu()),
        ]
        return items

    def _model_action(self, name: str):
        # pystray only accepts actions taking (icon, item); bind the name here.
        def action(icon, item) -> None:
            self._pick_model(icon, name)

        return action

    def _pick_rules(self, icon) -> None:
        cfg = self.engine.cfg.cleanup
        if cfg.method == "rules":
            return
        cfg.method = "rules"
        try:
            from .config import save_setting

            save_setting("cleanup", "method", "rules")
        except OSError as exc:
            log.warning("could not save cleanup method: %s", exc)
        log.info("cleanup switched to algorithmic")
        # Hand the cleanup model's VRAM back; nothing will use it now.
        threading.Thread(
            target=self.engine.cleaner.unload, daemon=True, name="localflow-model-unload"
        ).start()
        icon.update_menu()

    def _pick_model(self, icon, name: str) -> None:
        cfg = self.engine.cfg.cleanup
        if name == cfg.model and cfg.method != "rules":
            return
        try:
            from .config import save_setting

            save_setting("cleanup", "model", name)
            save_setting("cleanup", "method", "llm")
        except OSError as exc:
            log.warning("could not save cleanup model: %s", exc)
        cfg.method = "llm"
        previous = self.engine.cfg.cleanup.model
        self.engine.cfg.cleanup.model = name  # menu and overlay see it immediately
        # Unload + load can take tens of seconds; never block the tray thread.
        threading.Thread(
            target=self.engine.cleaner.switch_model, args=(name, previous), daemon=True,
            name="localflow-model-switch",
        ).start()
        icon.update_menu()

    def _preview(self, choice: str) -> None:
        if self.sounds is not None:
            from .sounds import play

            play(self.sounds.sound(choice))

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

    def _toggle_cleanup(self) -> None:
        self.engine.cfg.cleanup.enabled = not self.engine.cfg.cleanup.enabled
        self.engine.cleaner.cfg.enabled = self.engine.cfg.cleanup.enabled

    def _quit(self) -> None:
        log.info("quitting")
        _exit_if_stuck()
        if self._on_quit:
            self._on_quit()
        self.icon.stop()

    def stop(self) -> None:
        """Quit as if from the menu (tray icon removed properly). Any thread."""
        self._quit()

    def notify(self, message: str, title: str = "LocalFlow") -> None:
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
