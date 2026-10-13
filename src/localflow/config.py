"""Configuration loading. TOML on disk, dataclasses in memory."""

from __future__ import annotations

import secrets
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
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


@dataclass
class WhisperConfig:
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "float16"
    #: "" enables autodetection, which costs a little latency and can misfire on
    #: short clips. Pin it unless you actually dictate in several languages.
    language: str = "en"
    beam_size: int = 1
    vad_filter: bool = True
    #: Keeps the model resident so the first word after idle is not slow.
    download_root: str = ""


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
    timeout: float = 90.0
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


@dataclass
class HotkeyConfig:
    #: pynput key names. Combos use angle brackets, e.g. "<ctrl>+<alt>+d".
    #: Each may also be a list of specs that share one on/off switch.
    #: Modifier-only chords ("<ctrl>+<cmd>") fire on a clean tap, on release.
    dictate: str | list[str] = "<f9>"
    dictate_raw: str | list[str] = "<f10>"
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
    #: Seconds between live updates (each is one Whisper pass on the GPU).
    live_interval: float = 1.0
    #: Only the most recent this-many seconds are re-transcribed each update.
    live_window: float = 30.0
    #: Words of the previous sentence kept (dimmed) once a new one starts.
    live_context_words: int = 5
    #: Hard cap on words shown; long sentences show "..." plus the newest words.
    live_max_words: int = 30


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
    #: 0.0-1.0, applied to the built-in sounds.
    volume: float = 0.5


@dataclass
class VocabularyConfig:
    #: Proper nouns and jargon Whisper mangles. Fed to Whisper as a prompt hint
    #: and to the cleanup model as a spelling authority.
    terms: list[str] = field(default_factory=list)
    #: Literal spoken -> written substitutions applied after cleanup.
    replacements: dict[str, str] = field(default_factory=dict)


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
    vocabulary: VocabularyConfig = field(default_factory=VocabularyConfig)


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


def save_setting(section: str, key: str, value: str, path: Path | None = None) -> None:
    """Set one string value in config.toml, leaving every other line untouched.

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
            lines[hit] = new_line
        else:
            # After the section's last non-blank line, so it stays inside it.
            insert = end
            while insert > start + 1 and not lines[insert - 1].strip():
                insert -= 1
            lines.insert(insert, new_line)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
