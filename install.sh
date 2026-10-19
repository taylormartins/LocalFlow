#!/bin/bash
# Golden Flow (localflow) installer for macOS.
#
#   bash install.sh            install, and start at login
#   bash install.sh --no-start install only
#
# 1. uv (Astral's Python manager) if it's missing, then Python 3.12 and every
#    dependency into .venv (no admin needed).
# 2. config.toml from config.example.toml, if you don't have one.
# 3. The Whisper model (a one-time download).
# 4. Start at login (a launchd agent), and start it now.
#
# macOS will ask for permissions the first time they're needed, for the
# Python that runs Golden Flow: Microphone, Accessibility (it types the paste
# and presses keys), Input Monitoring (the dictation key). Allow them in
# System Settings > Privacy & Security, then quit and start it again.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\033[1;33m==>\033[0m %s\n' "$1"; }

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This installer is for macOS; on Windows run install.cmd." >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    say "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

say "Python and dependencies (into .venv)"
uv sync --no-dev --inexact

if [[ ! -f config.toml ]]; then
    say "config.toml from the template"
    cp config.example.toml config.toml
fi

say "the Whisper model (first time only; a few hundred MB)"
uv run localflow fetch-model || say "model download failed - it will be fetched on first use"

if ! command -v ollama >/dev/null 2>&1 && [[ ! -d /Applications/Ollama.app ]]; then
    say "Ollama isn't installed: cleanup runs on rules alone (fine). For LLM cleanup,"
    say "install it from https://ollama.com and run: ollama pull qwen2.5:7b"
fi

# The installed version, for the updater ("did the last install finish?").
git rev-parse HEAD > .installed 2>/dev/null || true

if [[ "${1:-}" != "--no-start" ]]; then
    say "start at login, and now"
    uv run localflow install-startup
fi

say "done. Hold Right Option to dictate. Settings: the crown in the menu bar."
