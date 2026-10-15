<#
.SYNOPSIS
    Update LocalFlow to the latest version and restart it. Your settings,
    phrases and models are kept.

.DESCRIPTION
    1. Fetches the latest code from GitHub. Works from any checkout, including
       one left on an old release tag (a "detached HEAD").
    2. Runs install.ps1 for the rest: dependencies, any new models, config
       kept as is. No admin prompt if the startup tasks already exist.
    3. Restarts LocalFlow.

    Run from the tray (Update LocalFlow), by double-clicking update.cmd, or:
        powershell -NoProfile -ExecutionPolicy Bypass -File update.ps1

    Exit codes: 0 updated, 3 already up to date, 1 failed (see update.log).
#>
[CmdletBinding()]
param(
    # Update even if already on the latest commit (re-run the install steps).
    [switch]$Force,
    # For testing against another checkout: which folder to update, and leave
    # the startup tasks and the running LocalFlow alone.
    [string]$Root = $PSScriptRoot,
    [switch]$NoStartup,
    [switch]$NoRestart
)

$ErrorActionPreference = 'Continue'
$root = (Resolve-Path $Root).Path
$logFile = Join-Path $root 'update.log'
# The tray app launches this detached and can't see the exit code, so the
# outcome is also written here for it to read: "<code> <message>".
$resultFile = Join-Path $root 'update.result'
Set-Location $root
Remove-Item $resultFile -ErrorAction SilentlyContinue

function Log($msg) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding ascii
}
function Done([int]$code, $msg) {
    Set-Content -Path $resultFile -Value "$code $msg" -Encoding ascii
    exit $code
}
function Fail($msg) { Log "FAILED: $msg"; Done 1 $msg }
function Stop-LocalFlow {
    # Ask it to quit first (it removes its own tray icon), then make sure.
    $find = {
        @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'localflow\.cli' -or (-not $_.CommandLine -and -not $_.ExecutablePath) })
    }
    if (& $find) {
        $quit = Join-Path $root '.quit'
        Set-Content -Path $quit -Value 'quit' -Encoding ascii
        $deadline = (Get-Date).AddSeconds(8)
        while ((Get-Date) -lt $deadline -and (& $find)) { Start-Sleep -Milliseconds 250 }
        Remove-Item $quit -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'localflow\.cli|restart\.ps1' -and $_.ProcessId -ne $PID } |
        ForEach-Object { taskkill /F /T /PID $_.ProcessId 2>$null | Out-Null }
}
function Start-LocalFlow {
    schtasks /query /tn 'LocalFlow' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { schtasks /run /tn 'LocalFlow' | Out-Null }
    else { Start-Process wscript.exe -ArgumentList ('"{0}"' -f (Join-Path $root 'restart.vbs')) }
}

Log '--- update started'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail 'git is not installed' }
if (-not (Test-Path (Join-Path $root '.git'))) {
    Fail 'this folder is not a git checkout (downloaded as a ZIP?). Download the new ZIP from https://github.com/taylormartins/LocalFlow and run install.cmd'
}

# Behind Zscaler-style proxies git trusts the Windows store via schannel, so
# no certificate setup is needed here.
git fetch --tags --prune origin 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE) { Fail 'could not reach GitHub (git fetch)' }

$before = (git rev-parse HEAD).Trim()
$target = (git rev-parse origin/main).Trim()
if ($before -eq $target -and -not $Force) {
    Log 'already up to date'
    Done 3 'already up to date'
}

# Tracked files edited by hand would be overwritten: refuse rather than lose
# them. config.toml, models\ and wake\ are untracked, so they are never touched.
$dirty = git status --porcelain --untracked-files=no
if ($dirty) {
    Log 'local changes to LocalFlow files:'
    $dirty | ForEach-Object { Log "  $_" }
    Fail 'commit or discard those changes (git stash), then update again'
}

# -B: also moves a checkout off an old tag (detached HEAD) onto main.
git checkout -B main origin/main 2>&1 | ForEach-Object { Log "  $_" }
if ($LASTEXITCODE) { Fail 'git checkout failed' }
git branch --set-upstream-to=origin/main main 2>&1 | Out-Null
Log ("code {0} -> {1}" -f $before.Substring(0, 7), $target.Substring(0, 7))

# New dependencies mean replacing files the running app has open: stop it
# first. Otherwise it keeps running until the restart at the end.
git diff --quiet $before $target -- uv.lock pyproject.toml .python-version
$depsChanged = $LASTEXITCODE -ne 0
if ($depsChanged -and -not $NoRestart) { Log 'dependencies changed: stopping LocalFlow for the install'; Stop-LocalFlow }

# The install steps (dependencies, models, startup) without relaunching: we
# restart it ourselves below, once everything has succeeded.
$installArgs = @('-NoLaunch', '-Quiet')
if ($NoStartup) { $installArgs += '-NoStartup' }
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'install.ps1') @installArgs 2>&1 |
    ForEach-Object { Log "  $_" }
if ($LASTEXITCODE) {
    if ($depsChanged -and -not $NoRestart) { Start-LocalFlow }  # we stopped it; bring it back regardless
    Fail 'install step failed - see update.log'
}

if (-not $NoRestart) { Start-LocalFlow }
Log 'updated and restarted'
Done 0 'updated'

