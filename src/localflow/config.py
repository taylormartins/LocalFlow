"""Configuration loading. TOML on disk, dataclasses in memory."""

from __future__ import annotations

import secrets
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass

from .platform import IS_MAC
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.toml"


@dataclass
class AudioConfig:
    #: Case-insensitive substring of the input device name; "" = Windows default.
    #: Some interfaces report different names per host API ("Focusrite USB" /
    #: "USB Audio"), so match on the part they share.
    device: str = ""
    #: Whisper resamples everything to 16 kHz anyway; capture there directly.
    samplerate: int = 16000
    channels: int = 1
    #: Hard stop so a stuck hotkey cannot fill memory.
    max_seconds: int = 300
    #: Recordings shorter than this are discarded as accidental key taps.
    min_seconds: float = 0.35
    #: Boost quiet input before transcription. 1.0 disables.
    gain: float = 1.0
    #: Mic boost on top of `gain`, also in the tray menu: "none", "low" (+6 dB),
    #: "medium" (+12 dB), "high" (+18 dB), or "auto" (measures each recording
    #: and lifts quiet speech, up to +26 dB; never turns anything down).
    boost: str = "none"


@dataclass
class WhisperConfig:
    model: str = "large-v3-turbo"
    #: No CUDA on a Mac: the CPU with int8 (fast on Apple silicon).
    device: str = "cpu" if IS_MAC else "cuda"
    compute_type: str = "int8" if IS_MAC else "float16"
    #: "" enables autodetection, which costs a little latency and can misfire on
    #: short clips. Pin it unless you actually dictate in several languages.
    language: str = "en"
    beam_size: int = 1
    vad_filter: bool = True
    #: Keeps the model resident so the first word after idle is not slow.
    download_root: str = ""
    #: Model mirror for networks that block huggingface.co (an internal
    #: Artifactory/Nexus proxy, say). "" = huggingface.co, or $HF_ENDPOINT.
    hf_endpoint: str = ""


@dataclass
class CleanupConfig:
    enabled: bool = True
    #: "llm" = Ollama (best), with the rules as fallback when it is unavailable.
    #: "rules" = algorithmic cleanup only (rules.py): no Ollama, no GPU, ~2 ms.
    method: str = "llm"
    host: str = "http://127.0.0.1:11434"
    #: 3b is ~1.4x faster but stops resolving self-corrections and spoken
    #: formatting, which is most of the value. See config.toml for the numbers.
    model: str = "qwen2.5:7b"
    #: Generous, because a cold Ollama load under VRAM pressure can take ~30s.
    #: The warm path is well under a second, so this only bites on first use.
    #: Seconds the LLM gets per dictation before the rules clean it instead.
    #: Was 90: when Ollama got into trouble a dictation sat for 45 s.
    timeout: float = 25.0
    #: Skip the LLM entirely when the transcript has no visible disfluency.
    #: Turns short, crisp dictation into a zero-latency path.
    skip_if_clean: bool = True
    #: How long Ollama holds the model in VRAM after a request.
    keep_alive: str = "30m"
    #: Skip the LLM for very short utterances ("yes", "next line") where a
    #: rewrite is all risk and no benefit.
    min_chars: int = 12
    #: Extra house style, appended to the built-in prompt.
    instructions: str = ""


@dataclass
class InjectConfig:
    #: "paste" is near-instant and handles Unicode; "type" is slower but works
    #: in the rare app that blocks programmatic paste (some terminals, games).
    method: str = "paste"
    restore_clipboard: bool = True
    paste_delay: float = 0.08
    type_interval: float = 0.004
    #: Also leave the result on the clipboard when pasting fails.
    clipboard_fallback: bool = True
    #: Where the text goes. "focus" = whichever window has focus when it's
    #: ready. "start" = the window you were in when the dictation began, so
    #: you can click around while talking; a "submit" lands there too, and
    #: you're returned to where you'd got to. Toggle: tray > Paste where I started.
    paste_to: str = "focus"
    #: Paste into a terminal as one line. Windows Terminal stops a multi-line
    #: paste with a "you are about to paste multiple lines" warning, so a
    #: long dictation (which the cleanup splits into paragraphs) never got
    #: in on its own, and a "submit" only clicked through the warning.
    terminal_single_line: bool = True


@dataclass
class HotkeyConfig:
    #: pynput key names. Combos use angle brackets, e.g. "<ctrl>+<alt>+d".
    #: Each may also be a list of specs that share one on/off switch.
    #: Modifier-only chords ("<ctrl>+<cmd>") fire on a clean tap, on release.
    #: (On a Mac: hold Right Option; with Shift for raw.)
    dictate: str | list[str] = "<alt_r>" if IS_MAC else "<f9>"
    dictate_raw: str | list[str] = "<shift>+<alt_r>" if IS_MAC else "<f10>"
    #: "hold" = press and hold to record (push-to-talk).
    #: "toggle" = tap to start, tap to stop.
    mode: str = "hold"
    cancel: str = "<esc>"


@dataclass
class ServerConfig:
    #: Off unless asked for: it listens on the network.
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8123
    #: Shared secret for the phone client. Empty string disables auth entirely,
    #: which is only safe on a trusted LAN.
    token: str = ""


@dataclass
class OverlayConfig:
    #: Floating pill above the taskbar: live level bars while recording, a
    #: spinner while processing. Never takes focus, never intercepts clicks.
    enabled: bool = True
    #: Pixels between the pill and the top of the taskbar (at 100% scaling).
    margin: int = 18
    #: RMS above which a block counts as speech and its bar is drawn bright.
    #: Measured on a USB audio interface 2026-10-11: room noise median 0.0032, max 0.0056
    #: per block. Speech blocks sit well above 0.01.
    speech_rms: float = 0.009
    #: Live transcript under the waveform while you speak. Display only - the
    #: pasted text still comes from the full transcription after you stop.
    live_text: bool = True
    #: The pill's look: "royal" (black and gold), "gemstone", "silver", "smooth",
    #: "modern", "fire", "ice", "classic" (the original), "matrix", "doodle", "newspaper",
    #: or one you built in Settings > Theme builder (kept in themes.json).
    theme: str = "royal"
    #: The new look: pixel themes (Silkscreen type, square beveled shapes, a
    #: segmented border, per the house style in C:/Code/design). Off returns
    #: to the legacy v2 themes above, untouched.
    pixel: bool = False
    #: Which pixel theme: "block" (plain castle gold), "knight", "wizard", "electric".
    pixel_theme: str = "block"
    #: How solid the pill is: 1.0 solid, down to 0.2 to see what's behind it.
    opacity: float = 1.0
    #: Numbered click labels (command mode "show labels"): 0.3 faint - 1.0 solid.
    label_opacity: float = 0.7
    #: A little burst in the theme's style where pasted text lands: flames,
    #: sparkles, ice, a ripple. So you can see where it went.
    paste_effect: bool = True
    #: Their numbers' size in pixels at 100% scaling (14 = about 10.5 pt).
    #: Never below LABEL_MIN (10 pt): smaller was unreadable in script fonts.
    label_size: int = 14
    #: Their look (and the window list's): "match" the pill's theme, "yellow"
    #: (the original yellow on black), or any theme's name.
    label_theme: str = "match"
    #: Seconds between live updates (each is one Whisper pass on the GPU).
    live_interval: float = 1.0
    #: Only the most recent this-many seconds are re-transcribed each update.
    live_window: float = 30.0
    #: Words of the previous sentence kept (dimmed) once a new one starts.
    live_context_words: int = 5
    #: Hard cap on words shown; long sentences show "..." plus the newest words.
    live_max_words: int = 30
    #: Without a GPU, the live text comes from this small model on the CPU
    #: (shared with always-on), reading only the last `live_cpu_window` seconds.
    #: "" turns the live text off on CPU-only PCs.
    live_cpu_model: str = "base.en"
    live_cpu_window: float = 10.0


@dataclass
class StyleConfig:
    #: Rewrite the cleaned text in a voice and/or emotional tone (one extra LLM
    #: pass). Pick both from the tray; they stack. "none" / "neutral" = off.
    character: str = "none"
    #: One of Plutchik's 24 levels (e.g. "annoyance", "anger", "rage") or "neutral".
    tone: str = "neutral"
    #: Your own characters, name -> description, e.g.
    #: pirate = "a salty pirate captain; nautical slang, gruff but cheerful"
    characters: dict[str, str] = field(default_factory=dict)
    #: Model for the style pass; "" = same as cleanup. gemma4:12B gives much
    #: stronger voices but ~2.8 s per pass warm vs ~0.35 s for qwen2.5:7b.
    model: str = ""


@dataclass
class SoundsConfig:
    #: Audible cues when recording starts and stops. Pick from the tray menu, or
    #: set a built-in name (tick, pop, faint pop, blip, click, chime up, chime down, soft,
    #: none) or a path to a .wav file.
    enabled: bool = True
    start: str = "tick"
    stop: str = "pop"
    #: After the text is pasted into the window.
    paste: str = "faint pop"
    #: A command is waiting for your answer (sleep's "goodnight", a "yes" to
    #: a plan, "change default mode"). "none" for silence.
    response: str = "triple click"
    #: A voice command was recognised ("submit" after the stop phrase, any
    #: command in command mode): like hearing a button press. "none" for silence.
    command: str = "click"
    #: 0.0-1.0, applied to the built-in sounds.
    volume: float = 0.5


@dataclass
class AlwaysOnConfig:
    #: Listen continuously; say the start phrase to begin dictating and the
    #: stop phrase to finish. Set the phrases up from the tray first.
    enabled: bool = False
    #: Small model that only listens for the phrases, on the CPU so the GPU
    #: stays free. base.en hears names far better than tiny.en for ~2x the cost.
    model: str = "base.en"
    #: Stop on our own if nothing has been said for this long while dictating,
    #: so a forgotten stop phrase can't record the room for ten minutes.
    #: 0 = never (the stop phrase, a hotkey or Esc only).
    idle_stop_seconds: float = 45.0
    #: How close every word of a heard phrase must be to a known spelling, 0-1.
    #: Lower catches more mishearings and more lookalikes ("Mary told" for
    #: "marigold"); add spellings in setup rather than lowering this.
    accept: float = 0.85
    #: How loosely a heard phrase must sound like your setup takes, 0-1.
    #: Raise it when a phrase reads right in the log but is "vetoed" (a poor
    #: or boosted mic); 1.0 all but turns the voice check off. Settings > Listening.
    voice_sensitivity: float = 0.25
    #: Extra spellings for voice commands, name -> spellings (see commands.py
    #: for the names and the built-in ones, e.g. submit = "submit", "press enter").
    commands: dict[str, list[str]] = field(default_factory=dict)
    #: Seconds in which a command said after a dictation counts: from the stop
    #: phrase, and again from the moment the text is pasted.
    command_window: float = 5.0
    #: Your own escape words, besides the built-in ones: throw a dictation
    #: away after the stop phrase ("cancel", "never mind"...), and leave
    #: command mode ("exit", "done"...). Settings > Listening.
    cancel_words: list[str] = field(default_factory=list)
    exit_words: list[str] = field(default_factory=list)
    #: Quick mode (off by default): one breath does it all. "<start phrase>
    #: continue over" pastes "continue"; "... continue send" pastes it and
    #: presses Enter. The end word also ends a longer dictation, but only as
    #: the last thing said before a pause, so "move it over here" doesn't.
    quick: bool = False
    quick_stop: list[str] = field(default_factory=lambda: ["over"])
    quick_send: list[str] = field(default_factory=lambda: ["send"])
    #: The do word, quick mode or not: "<start phrase> do enter" runs that
    #: one command and that's it, no command mode left open; "<start> do", a
    #: pause, then the command works too. Misheard as "to", "undo", "due" or
    #: "dew", it still counts when what follows is a command.
    do_words: list[str] = field(default_factory=lambda: ["do"])
    #: Quick mode's shorthand, each "word = command [argument]": "go = submit",
    #: "nope = scratch", "chrome = switch chrome". Only while quick mode is on.
    code_words: list[str] = field(default_factory=list)
    #: Your own words alongside the built-in ones, "group = phrase": groups are
    #: command mode, dictation mode, yes, no, remember, forget, edit mode,
    #: default mode, goodnight. Settings > Listening > Your own words.
    phrase_words: list[str] = field(default_factory=list)
    #: Your own ways to say commands, "phrase = command" (slots work:
    #: "bring up {name} = switch"). Settings > Listening > Your own words.
    command_phrases: list[str] = field(default_factory=list)
    #: Command mode ends after this many seconds without a command; 0 = never.
    #: Applies whichever mode is the default. "command timeout two minutes".
    command_mode_seconds: float = 30.0
    #: "dictation": wait for the start phrase; command mode is a visit.
    #: "command": always in command mode, back to it after every dictation.
    #: Say "change default mode" in command mode, or pick it in the tray.
    default_mode: str = "dictation"
    #: In command mode, ask the cleanup LLM (if it's in use and reachable) to
    #: work out requests the built-in commands don't cover.
    command_llm: bool = True


@dataclass
class HistoryConfig:
    #: Save every dictation to history.jsonl (this PC only) for the history
    #: window: tray > Dictation history..., or "show history" in command mode.
    enabled: bool = True
    #: Oldest entries are dropped past this many.
    max_items: int = 500


@dataclass
class VocabularyConfig:
    #: Proper nouns and jargon Whisper mangles. Fed to Whisper as a prompt hint
    #: and to the cleanup model as a spelling authority.
    terms: list[str] = field(default_factory=list)
    #: Literal spoken -> written substitutions applied after cleanup.
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass
class CameraConfig:
    #: Camera mode, part one: which screen you're looking at (docs/CAMERA_MODE.md).
    #: Off by default; needs camera support (Settings > Camera > Install).
    #: Frames are read, turned into a direction and dropped; nothing is saved
    #: but the calibration.
    enabled: bool = False
    #: Which cameras, and the screen each sits on: "0:1" = camera 0 on screen 1.
    #: One camera per screen tells looking left from right best.
    cameras: list[str] = field(default_factory=lambda: ["0:1"])
    #: The start phrase only counts while you're looking at a screen.
    gate_start: bool = True
    #: Command mode doesn't time out while you're looking at a screen.
    hold_commands: bool = True
    #: Frames a second per camera (CPU use rises with it).
    fps: float = 10.0


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    sounds: SoundsConfig = field(default_factory=SoundsConfig)
    style: StyleConfig = field(default_factory=StyleConfig)
    always_on: AlwaysOnConfig = field(default_factory=AlwaysOnConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    vocabulary: VocabularyConfig = field(default_factory=VocabularyConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)


def _apply(target: Any, data: dict[str, Any], path: str = "") -> list[str]:
    """Overlay a parsed TOML table onto a dataclass. Returns unknown-key warnings."""
    warnings: list[str] = []
    known = {f.name: f for f in fields(target)}
    for key, value in data.items():
        if key not in known:
            warnings.append(f"unknown config key: {path}{key}")
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            warnings.extend(_apply(current, value, f"{path}{key}."))
        else:
            setattr(target, key, value)
    return warnings


def load(path: Path | None = None) -> tuple[Config, list[str]]:
    """Load config from disk, falling back to defaults for anything absent."""
    path = path or CONFIG_PATH
    cfg = Config()
    warnings: list[str] = []

    if path.exists():
        with path.open("rb") as fh:
            warnings = _apply(cfg, tomllib.load(fh))
    else:
        warnings.append(f"no config file at {path}; using defaults")

    if cfg.server.enabled and not cfg.server.token:
        warnings.append(
            "server.token is empty - the phone endpoint is unauthenticated. "
            f"Set one in {path} (suggestion: {secrets.token_urlsafe(24)})"
        )

    return cfg, warnings


def save_setting(
    section: str, key: str, value: str | bool | list[str], path: Path | None = None
) -> None:
    """Set one string (or list of strings) in config.toml, leaving every other line untouched.

    Used by the tray menu. A targeted line edit rather than a TOML rewrite, so
    the hand-written comments in config.toml survive.
    """
    import json
    import re

    path = path or CONFIG_PATH
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_line = f"{key} = {json.dumps(value)}"  # JSON strings are valid TOML strings

    start = next((i for i, l in enumerate(lines) if l.strip() == f"[{section}]"), None)
    if start is None:
        lines += ["", f"[{section}]", new_line]
    else:
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        hit = next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)
        if hit is not None:
            # A hand-written array can span lines; replace all of it.
            last = hit
            if "[" in lines[hit].split("=", 1)[1] and "]" not in lines[hit].split("#", 1)[0]:
                while last + 1 < end and "]" not in lines[last]:
                    last += 1
            lines[hit : last + 1] = [new_line]
        else:
            # After the section's last non-blank line, so it stays inside it.
            insert = end
            while insert > start + 1 and not lines[insert - 1].strip():
                insert -= 1
            lines.insert(insert, new_line)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
