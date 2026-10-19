#!/bin/bash
# Update Golden Flow (localflow) on macOS to the latest version and restart it.
# The tray's Update runs this detached; it writes "<code> <message>" to
# update.result for the tray to read (0 updated, 1 failed, 3 up to date),
# the same as update.ps1 on Windows. Settings, phrases and models are
# untracked files and are never touched.
set -uo pipefail
cd "$(dirname "$0")"
LOG=update.log
RESULT=update.result
rm -f "$RESULT"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOG"; }
done_() { printf '%s %s\n' "$1" "$2" > "$RESULT"; exit "$1"; }
fail() { log "FAILED: $1"; done_ 1 "$1"; }

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
command -v git >/dev/null || fail "git is not installed"
git rev-parse --git-dir >/dev/null 2>&1 || fail "this folder is not a git checkout"
log "update: fetching"
git fetch --tags --prune origin >>"$LOG" 2>&1 || fail "could not reach GitHub (git fetch)"
before=$(git rev-parse HEAD)
target=$(git rev-parse origin/main)
installed=$(head -n1 .installed 2>/dev/null || true)
if [[ "$before" == "$target" && "$installed" == "$target" ]]; then
    log "already up to date"
    done_ 3 "already up to date"
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    fail "commit or discard local changes (git stash), then update again"
fi
git checkout -B main origin/main >>"$LOG" 2>&1 || fail "git checkout failed"
git branch --set-upstream-to=origin/main main >/dev/null 2>&1
log "code ${before:0:7} -> ${target:0:7}"
log "dependencies"
uv sync --no-dev --inexact >>"$LOG" 2>&1 || fail "the install step failed - see update.log"
git rev-parse HEAD > .installed
# Tells the restarted copy to say "Updated to ..." (it removes the file).
printf '%s %s\n' "${before:0:7}" "${target:0:7}" > .updated
log "restarting"
launchctl kickstart -k "gui/$(id -u)/com.goldenflow.localflow" >>"$LOG" 2>&1 \
    || log "not started at login (run: uv run localflow install-startup)"
log "updated and restarted"
done_ 0 "updated"
