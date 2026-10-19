"""Global push-to-talk hotkeys (Windows and Mac).

Two pynput constraints shape this module:

1. ``GlobalHotKeys`` only fires on activation, which cannot express "record while
   held", so press and release are tracked directly.
2. ``suppress_event()`` raises an exception that aborts pynput's event
   conversion, so the ordinary ``on_press``/``on_release`` callbacks never run
   for a key we suppress. Everything therefore happens inside the low-level
   event filter, keyed on Windows virtual-key codes.

The filter runs on the keyboard hook thread, where any delay stalls input system
wide, so it only does set arithmetic and hands the actual work to a worker.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from pynput import keyboard

from .platform import IS_MAC

log = logging.getLogger(__name__)

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

DOWN_MESSAGES = frozenset({WM_KEYDOWN, WM_SYSKEYDOWN})
UP_MESSAGES = frozenset({WM_KEYUP, WM_SYSKEYUP})

#: Windows reports the physical left/right modifier; hotkey specs name the
#: generic one. Collapse both onto the generic code before comparing.
VK_NORMALISE = {
    160: 16, 161: 16,  # L/R SHIFT  -> SHIFT
    162: 17, 163: 17,  # L/R CONTROL -> CONTROL
    164: 18, 165: 18,  # L/R MENU (alt) -> MENU
    92: 91,            # RWIN -> LWIN
}

MODIFIER_VKS = frozenset({16, 17, 18, 91})
#: The Mac's dictation key: Right Option, kept apart from Option (18) so the
#: left one still types accents, and not a modifier here, so holding it alone
#: records (push-to-talk).
VK_RIGHT_OPTION = 0xA5
#: pynput key names -> the Windows virtual-key codes this module works in. On
#: a Mac pynput gives names (its own key codes differ from Windows').
MAC_VKS = {
    "alt": 18, "alt_l": 18, "alt_r": VK_RIGHT_OPTION, "alt_gr": VK_RIGHT_OPTION,
    "cmd": 91, "cmd_l": 91, "cmd_r": 91, "ctrl": 17, "ctrl_l": 17, "ctrl_r": 17,
    "shift": 16, "shift_l": 16, "shift_r": 16, "caps_lock": 20, "esc": 27, "space": 32,
    "enter": 13, "tab": 9, "backspace": 8, "delete": 46, "up": 38, "down": 40,
    "left": 37, "right": 39, "home": 36, "end": 35, "page_up": 33, "page_down": 34,
    **{f"f{n}": 0x6F + n for n in range(1, 21)},
}
VK_LWIN = 91

#: Unassigned virtual key. Tapped while a Win-key chord is held so Windows sees
#: "another key was pressed" and does not open Start when Win is released - the
#: same trick AutoHotkey uses. Ignored by our own filter.
VK_MASK = 0xE8


def normalise_vk(vk: int | None) -> int | None:
    return VK_NORMALISE.get(vk, vk) if vk is not None else None


def parse_spec(spec: str) -> frozenset:
    """Parse a pynput hotkey string such as ``<ctrl>+<alt>+d`` into canonical keys."""
    try:
        return frozenset(keyboard.HotKey.parse(spec))
    except ValueError as exc:
        raise ValueError(f"invalid hotkey {spec!r}: {exc}") from exc


def key_vk(key) -> int | None:
    """Best-effort Windows virtual-key code for a parsed pynput key.

    ``Key`` members wrap a KeyCode in ``.value``; ``KeyCode.from_char`` yields a
    char with no vk, so letters and digits map to their ASCII code, which is what
    Windows uses for those virtual keys.
    """
    if IS_MAC:
        name = getattr(key, "name", None)
        if name is None:
            # A parsed spec can give the Mac's raw key code ("<alt_r>" is
            # KeyCode 61), not the Key: look its name up.
            name = _mac_key_names().get(getattr(getattr(key, "value", key), "vk", None))
        if name in MAC_VKS:
            return MAC_VKS[name]
        char = getattr(key, "char", None)
        return ord(char.upper()) if char and len(char) == 1 and char.isalnum() else None
    inner = getattr(key, "value", key)
    vk = getattr(inner, "vk", None)
    if vk is None:
        char = getattr(inner, "char", None)
        if char and len(char) == 1 and char.isalnum():
            vk = ord(char.upper())
    return normalise_vk(vk)


def _mac_key_names() -> dict:
    """The Mac's key codes -> pynput Key names (built once)."""
    names = getattr(_mac_key_names, "cache", None)
    if names is None:
        names = {}
        for k in keyboard.Key:
            vk = getattr(k.value, "vk", None)
            if vk is not None:
                names.setdefault(vk, k.name)
        _mac_key_names.cache = names
    return names


def spec_to_vks(spec: str) -> frozenset[int]:
    vks = set()
    for key in parse_spec(spec):
        vk = key_vk(key)
        if vk is None:
            raise ValueError(f"cannot resolve a virtual-key code for {key!r} in {spec!r}")
        vks.add(vk)
    return frozenset(vks)


#: Dictation keys selectable from the tray: name -> (dictate, dictate_raw).
#: Raw (verbatim, no cleanup) is the same key with Shift, except F9's F10.
KEY_PRESETS: dict[str, tuple[str, str]] = {
    "Caps Lock": ("<caps_lock>", "<shift>+<caps_lock>"),
    "Ctrl+Win": ("<ctrl>+<cmd>", "<ctrl>+<cmd>+<shift>"),
    "F9": ("<f9>", "<f10>"),
    "Scroll Lock": ("<scroll_lock>", "<shift>+<scroll_lock>"),
    "Pause/Break": ("<pause>", "<shift>+<pause>"),
}
if IS_MAC:  # Caps Lock toggles in hardware on a Mac, and there's no Scroll Lock
    KEY_PRESETS = {
        "Right Option": ("<alt_r>", "<shift>+<alt_r>"),
        "Ctrl+Cmd": ("<ctrl>+<cmd>", "<ctrl>+<cmd>+<shift>"),
        "F5": ("<f5>", "<f6>"),
    }

VK_CAPITAL = 0x14
#: dwExtraInfo stamped on keys LocalFlow injects itself, so the hook lets them
#: through. Without it, turning Caps Lock off from code would be swallowed by
#: the very Caps Lock binding that made it impossible to turn off by hand.
OWN_INPUT = 0x4C46  # "LF"


def _tap(vk: int) -> None:
    if IS_MAC:
        return  # only needed for Windows' Start menu and Caps Lock tricks
    import ctypes

    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, OWN_INPUT)
    user32.keybd_event(vk, 0, 2, OWN_INPUT)  # KEYEVENTF_KEYUP


def _tap_mask_key() -> None:
    _tap(VK_MASK)


def caps_lock_on() -> bool:
    if IS_MAC:
        return False  # not a dictation key on a Mac: nothing gets stuck
    import ctypes

    return bool(ctypes.windll.user32.GetKeyState(VK_CAPITAL) & 1)


def caps_lock_off() -> bool:
    """Turn Caps Lock off if it is on. Returns whether it had to.

    A Caps Lock dictation key swallows the key, so Caps Lock left on before
    LocalFlow started (or by a tap that slipped through) would otherwise stay
    on for good.
    """
    if not caps_lock_on():
        return False
    _tap(VK_CAPITAL)
    log.info("turned Caps Lock off")
    return True


@dataclass
class Binding:
    spec: str
    on_press: Callable[[], None]
    on_release: Callable[[], None] | None = None
    #: "hold" fires on_press at key-down and on_release at key-up.
    #: "toggle" alternates on_press / on_release on successive taps.
    mode: str = "hold"
    suppress: bool = True
    #: Toggle bindings sharing a group act as one switch: start with Caps Lock,
    #: stop with Ctrl+Win. Empty means the binding is its own group.
    group: str = ""
    vks: frozenset[int] = field(init=False)
    triggers: frozenset[int] = field(init=False)
    #: Modifiers only (e.g. Ctrl+Win). These cannot fire on key-down - Ctrl+Win
    #: is also the start of Ctrl+Win+D and Ctrl+Win+Left - so they fire on
    #: release, and only if nothing else was pressed while they were held.
    chord: bool = field(init=False)

    def __post_init__(self) -> None:
        self.vks = spec_to_vks(self.spec)
        # The non-modifier keys. Only these are suppressed - blanket-suppressing
        # Ctrl would break the entire keyboard.
        self.triggers = frozenset(v for v in self.vks if v not in MODIFIER_VKS)
        self.chord = not self.triggers
        if self.chord and len(self.vks) < 2:
            raise ValueError(f"{self.spec!r} is a single modifier; use two or add a regular key")
        if not self.group:
            self.group = self.spec


class HotkeyManager:
    def __init__(self) -> None:
        self._bindings: list[Binding] = []
        self._pressed: set[int] = set()
        self._active: Binding | None = None
        self._toggled: Binding | None = None
        #: Modifier chord whose keys are exactly what is held right now.
        self._chord: Binding | None = None
        self._chord_spoiled = False
        self._listener: keyboard.Listener | None = None
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._worker: threading.Thread | None = None

    def bind(self, binding: Binding) -> None:
        self._bindings.append(binding)

    def replace(self, bindings: list[Binding]) -> None:
        """Swap every binding while running (the tray's key picker).

        One list assignment, which the hook thread sees atomically. A toggle
        recording that is running stays stoppable: _toggled is matched by
        group, and the new bindings use the same groups.
        """
        self._bindings = list(bindings)
        self._active = None
        self._chord = None
        log.info("hotkeys now: %s", ", ".join(b.spec for b in bindings) or "none")

    @property
    def specs(self) -> list[str]:
        return [b.spec for b in self._bindings]

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, fn: Callable[[], None] | None) -> None:
        if fn is not None:
            self._queue.put(fn)

    def _drain(self) -> None:
        while True:
            fn = self._queue.get()
            if fn is None:
                return
            try:
                fn()
            except Exception:
                log.exception("hotkey handler failed")

    # -- state machine -----------------------------------------------------

    def _match(self) -> Binding | None:
        """The most specific key binding fully satisfied by the currently-held keys."""
        candidates = [b for b in self._bindings if not b.chord and b.vks <= self._pressed]
        return max(candidates, key=lambda b: len(b.vks)) if candidates else None

    def _fire(self, binding: Binding) -> None:
        """Run a binding's press action: toggle, hold-start, or one-shot."""
        if binding.mode == "action":
            # Fire-and-forget, and clear any armed toggle or held binding so a
            # cancelled recording cannot leave the state machine mid-dictation.
            self._toggled = None
            self._active = None
            self._dispatch(binding.on_press)
            return

        if binding.mode == "toggle":
            if self._toggled is not None and self._toggled.group == binding.group:
                previous, self._toggled = self._toggled, None
                self._dispatch(previous.on_release)
            else:
                # Tapping a different toggle while one is armed must close the
                # first, or F9-then-F10 leaves a recording running that neither
                # key can stop.
                if self._toggled is not None:
                    previous, self._toggled = self._toggled, None
                    self._dispatch(previous.on_release)
                self._toggled = binding
                self._dispatch(binding.on_press)
        elif self._active is None:
            self._active = binding
            self._dispatch(binding.on_press)

    def _handle_down(self, vk: int) -> None:
        if vk in self._pressed:
            return  # auto-repeat
        self._pressed.add(vk)

        # Modifier chords: arm when the held set is exactly a chord, spoil if
        # anything else joins (Ctrl+Win+D must stay a desktop shortcut).
        chord = next((b for b in self._bindings if b.chord and b.vks == self._pressed), None)
        if chord is not None:
            self._chord, self._chord_spoiled = chord, False
            if VK_LWIN in chord.vks:
                self._dispatch(_tap_mask_key)
            if chord.mode == "hold":
                self._fire(chord)
        elif self._chord is not None:
            self._chord_spoiled = True

        binding = self._match()
        if binding is not None:
            self._fire(binding)

    def _handle_up(self, vk: int) -> None:
        chord = self._chord
        if chord is not None and vk in chord.vks:
            self._chord = None
            if chord.mode == "hold" and self._active is chord:
                self._active = None
                self._dispatch(chord.on_release)
            elif not self._chord_spoiled and chord.mode != "hold":
                self._fire(chord)

        self._pressed.discard(vk)
        active = self._active
        if active is not None and not active.chord and not active.vks <= self._pressed:
            self._active = None
            self._dispatch(active.on_release)

    def _should_suppress(self, vk: int) -> bool:
        """Suppress a trigger key only while its own modifiers are held."""
        for b in self._bindings:
            if b.suppress and vk in b.triggers and (b.vks - b.triggers) <= self._pressed:
                return True
        return False

    def _filter(self, msg, data) -> None:
        """Runs on the keyboard hook thread for every key event. Must stay cheap."""
        if self._listener is None:
            return None
        vk = normalise_vk(getattr(data, "vkCode", None))
        if vk is None or vk == VK_MASK or getattr(data, "dwExtraInfo", None) == OWN_INPUT:
            return None

        if msg in DOWN_MESSAGES:
            self._handle_down(vk)
        elif msg in UP_MESSAGES:
            self._handle_up(vk)
        else:
            return None

        if self._should_suppress(vk):
            # Raises internally: aborts pynput's conversion and blocks the key
            # system wide. Our handlers already ran above, which is why all the
            # state tracking lives in this filter rather than in on_press.
            self._listener.suppress_event()
        return None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._listener is not None:
            return
        self._worker = threading.Thread(target=self._drain, daemon=True, name="localflow-hotkey")
        self._worker.start()
        if IS_MAC:
            # No low-level filter on a Mac: pynput's callbacks drive the same
            # state machine. Keys aren't suppressed (Right Option on its own
            # types nothing anyway).
            self._listener = keyboard.Listener(
                on_press=lambda key: self._mac_key(key, True),
                on_release=lambda key: self._mac_key(key, False))
            self._listener.start()
            self._listener.wait()
            log.info("hotkeys active: %s", ", ".join(b.spec for b in self._bindings))
            return
        # on_press/on_release are deliberately no-ops; _filter drives everything.
        self._listener = keyboard.Listener(
            on_press=lambda key: None,
            on_release=lambda key: None,
            win32_event_filter=self._filter,
        )
        self._listener.start()
        self._listener.wait()
        log.info("hotkeys active: %s", ", ".join(b.spec for b in self._bindings))

    def _mac_key(self, key, down: bool) -> None:
        vk = key_vk(key)
        if vk is None:
            return
        if down:
            self._handle_down(vk)
        else:
            self._handle_up(vk)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self._queue.put(None)
        self._worker = None
        self._pressed.clear()
        self._active = None
        self._toggled = None
        self._chord = None

    def join(self) -> None:
        if self._listener is not None:
            self._listener.join()
