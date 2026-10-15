# Bring LocalFlow back to a known-good state: models drive present, Ollama up,
# and a fresh tray app with a live keyboard hook.
#
# Runs automatically at logon (scheduled task "LocalFlow") and on wake from
# sleep ("LocalFlow resume"). Both tasks run elevated: Windows won't deliver
# keystrokes to a normal-user keyboard hook while an admin window (e.g. an
# elevated Windows Terminal) has focus, so a non-elevated LocalFlow goes deaf
# there. To restart by hand: schtasks /run /tn "LocalFlow resume". Double-clicking
# restart.vbs also works, but runs non-elevated and cannot stop the elevated copy. Sleep is what breaks things - the low-level keyboard hook can be
# silently dropped, and Ollama has been found dead after resume - so restarting
# everything beats trying to detect which part broke.

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$logFile = Join-Path $root 'localflow.log'

function Log($msg) {
    $line = '{0} INFO    localflow.restart      {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

function Wait-Until([scriptblock]$test, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $test) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Test-Ollama {
    try { Invoke-RestMethod -TimeoutSec 3 http://127.0.0.1:11434/api/version | Out-Null; $true }
    catch { $false }
}

Log 'restart requested'

# Is Ollama in play at all? Not if it isn't installed, or config.toml picks the
# algorithmic cleanup (cleanup.method = "rules"). Then skip everything Ollama.
$ollamaApp = "$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe"
$configFile = Join-Path $root 'config.toml'
$rulesOnly = (Test-Path $configFile) -and
    ((Get-Content $configFile -Raw) -match '(?m)^\s*method\s*=\s*"rules"')
$useOllama = (Test-Path $ollamaApp) -and -not $rulesOnly
if (-not $useOllama) { Log 'ollama not used (not installed, or cleanup.method = "rules")' }

# If Ollama's models live on another drive (C:\Users\<you>\.ollama as a junction,
# or OLLAMA_MODELS pointing elsewhere), wait for it: USB drives can take a while
# to come back after resume, and without them Ollama answers every chat with a 400.
$modelsDir = (Get-Item "$env:USERPROFILE\.ollama" -Force -ErrorAction SilentlyContinue).Target | Select-Object -First 1
if (-not $modelsDir -and $env:OLLAMA_MODELS) { $modelsDir = $env:OLLAMA_MODELS }
if ($useOllama -and $modelsDir) {
    if (Wait-Until { Test-Path $modelsDir } 90) { Log "models drive ready ($modelsDir)" }
    else { Log "models drive missing ($modelsDir); cleanup will use the algorithmic rules" }
}

# Ollama installs a downloaded update only when its app starts - at logon, the
# same moment we load the model - and the installer then can't close Ollama and
# gives up, leaving it dead (2026-09-24, 2026-10-12). If a staged installer is
# NEWER than what's installed, let it finish before touching Ollama. A leftover
# installer for the current version is ignored, or every logon would wait.
$staged = if (-not $useOllama) { $null } else { Get-ChildItem "$env:LOCALAPPDATA\Ollama\updates_v2" -Recurse -Filter OllamaSetup.exe -ErrorAction SilentlyContinue | Select-Object -First 1 }
# ollama.exe carries no version resource; the installer's uninstall entry does.
$installed = (Get-ChildItem HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall -ErrorAction SilentlyContinue |
    Get-ItemProperty | Where-Object { $_.DisplayName -match '^Ollama' } | Select-Object -First 1).DisplayVersion
$stagedVer = if ($staged) { "$($staged.VersionInfo.ProductVersion)".Trim() } else { '' }
if ($stagedVer -and $installed -and [version]$stagedVer -gt [version]"$installed".Trim()) {
    Log ("ollama update {0} pending (installed {1}); waiting for its installer" -f $stagedVer, $installed)
    if (Wait-Until { Get-Process OllamaSetup* -ErrorAction SilentlyContinue } 120) {
        Wait-Until { -not (Get-Process OllamaSetup* -ErrorAction SilentlyContinue) } 300 | Out-Null
        Log 'ollama update finished'
    } else {
        Log 'ollama installer never started; carrying on'
    }
}

# At logon Ollama's own Startup shortcut is usually still bringing it up (or its
# updater is mid-install), so give a running process time before killing it.
if ($useOllama -and -not (Test-Ollama) -and (Get-Process 'ollama', 'OllamaSetup*' -ErrorAction SilentlyContinue)) {
    Log 'ollama starting; waiting for it'
    Wait-Until { Test-Ollama } 90 | Out-Null
}
if ($useOllama -and -not (Test-Ollama)) {
    Log 'ollama not responding; starting it'
    Get-Process 'ollama app', 'ollama', 'llama-server' -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Process $ollamaApp
    if (Wait-Until { Test-Ollama } 60) { Log 'ollama up' } else { Log 'ollama failed to start' }
}

# Kill the old instance: the venv pythonw launcher plus the real interpreter it
# spawns. This MUST succeed - an old instance survives sleep with a dead CUDA
# context, keeps its keyboard hook, and answers F9 with "CUDA failed with error
# unknown error". Don't find it via WMI (Get-CimInstance): straight after resume
# WMI can throw "System error" and return nothing, which silently left two
# instances running on 2026-09-27. Get-Process + taskkill /T need no WMI.
#
# If this script ever runs non-elevated (restart.vbs double-clicked), the
# elevated instance is invisible to it: its Path reads as empty and taskkill is
# denied. Treat an unreadable pythonw as ours so we refuse to stack a duplicate
# on top of it.
#
# The app runs as the base interpreter's real pythonw.exe (see below). An
# instance started the old way runs as the venv launcher, so match both.
$launcher = Join-Path $root '.venv\Scripts\pythonw.exe'
$baseHome = ((Get-Content (Join-Path $root '.venv\pyvenv.cfg')) -match '^home\s*=' -replace '^home\s*=\s*', '').Trim()
$pythonw = Join-Path $baseHome 'pythonw.exe'
function Get-OldInstances {
    # Prefer the command line: the base pythonw.exe is shared by every app on
    # that Python, so its path alone could match someone else's program.
    try {
        $ids = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction Stop |
            Where-Object {
                $_.CommandLine -match 'localflow\.cli' -or
                # Blank command line AND path: an elevated process seen from a
                # non-elevated run. Before this counted, such a run found
                # "nothing running" and started a second copy - one more live
                # tray icon per restart (seen on a work PC, 2026-10-14).
                (-not $_.CommandLine -and -not $_.ExecutablePath)
            } | ForEach-Object { $_.ProcessId })
        return @(Get-Process -Id $ids -ErrorAction SilentlyContinue)
    } catch {
        # WMI can fail right after resume (see above): fall back to the path.
        Get-Process pythonw -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $launcher -or $_.Path -eq $pythonw -or -not $_.Path }
    }
}
function Wait-Gone([int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline -and (Get-OldInstances)) { Start-Sleep -Milliseconds 250 }
    return -not (Get-OldInstances)
}

# Not elevated, but the startup tasks exist (restart.vbs double-clicked, or a
# launch from a non-admin installer): let the elevated task do the restart.
# Run from here, the new copy would lose its admin rights, and an elevated old
# copy can't be stopped from here anyway.
$me = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal $me).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
schtasks /query /tn 'LocalFlow resume' 2>$null | Out-Null
if (-not $isAdmin -and $LASTEXITCODE -eq 0) {
    Log 'not elevated; restarting through the elevated "LocalFlow resume" task'
    schtasks /run /tn 'LocalFlow resume' | Out-Null
    exit 0
}

# First ask nicely: the app watches for this file and quits on its own, which
# removes its tray icon (a killed process leaves its icon behind until the
# mouse passes over it). A file works across elevation; signals don't.
$quitFile = Join-Path $root '.quit'
if (Get-OldInstances) {
    Set-Content -Path $quitFile -Value (Get-Date -Format o) -Encoding ascii
    if (Wait-Gone 8) { Log 'old tray app quit cleanly' }
}
Remove-Item $quitFile -ErrorAction SilentlyContinue

for ($i = 0; $i -lt 5 -and (Get-OldInstances); $i++) {
    Get-OldInstances | ForEach-Object { taskkill /F /T /PID $_.Id 2>&1 | Out-Null }
    Start-Sleep -Seconds 1
}
if (Get-OldInstances) {
    # Still there: it runs as admin and we don't (restart.vbs double-clicked,
    # or a launch from a non-admin installer). Hand over to the elevated task,
    # which can stop it, rather than stacking a second copy on top.
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $admin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    schtasks /query /tn 'LocalFlow resume' 2>$null | Out-Null
    if (-not $admin -and $LASTEXITCODE -eq 0) {
        Log 'old tray app runs as admin; restarting through the elevated task instead'
        schtasks /run /tn 'LocalFlow resume' | Out-Null
        exit 0
    }
    Log ('could not stop old tray app (pid {0}) - it runs as admin. Use: schtasks /run /tn "LocalFlow resume"' -f ((Get-OldInstances).Id -join ','))
    exit 1
}

# Launch the base interpreter's pythonw.exe directly, NOT the venv's pythonw.exe.
# The venv one is a launcher that starts the console python.exe, and on this
# machine (Windows Terminal is the default terminal) that console showed up as a
# visible window titled "...\.venv\Scripts\pythonw.exe". Closing it killed the
# app, and the supervisor then reopened it. A GUI-subsystem pythonw has no console
# at all. __PYVENV_LAUNCHER__ is how the venv launcher tells the base interpreter
# which venv it is, so we set it ourselves: sys.prefix and site-packages come out
# exactly as with the launcher.
$env:__PYVENV_LAUNCHER__ = $launcher

# Stay alive as a supervisor: if the app crashes, start it again. Exit codes,
# measured 2026-10-01:
#   0          Quit from the tray        -> stop supervising
#   1          taskkill by a newer run   -> stop; that run supervises now
#   0xC0000409 native abort (CUDA etc.)  -> relaunch
# Python stacks from a crash land in crash.log (faulthandler, see cli.py).
$crashes = @()
while ($true) {
    $p = Start-Process -FilePath $pythonw -PassThru `
        -ArgumentList '-m', 'localflow.cli', 'run' -WorkingDirectory $root
    $null = $p.Handle   # without touching Handle, ExitCode comes back empty
    Log 'tray app relaunched'
    $p.WaitForExit()
    $code = $p.ExitCode
    if ($code -eq 0 -or $code -eq 1) { break }

    $crashes = @($crashes | Where-Object { $_ -gt (Get-Date).AddMinutes(-10) }) + (Get-Date)
    Log ('tray app crashed (exit 0x{0:X8}); see crash.log' -f $code)
    if ($crashes.Count -ge 3) { Log 'crashed 3 times in 10 minutes; not restarting again'; break }
    Start-Sleep -Seconds 3
}
