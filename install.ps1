<#
.SYNOPSIS
    Install LocalFlow on this PC: dependencies, config, Whisper model, start at login.

.DESCRIPTION
    Safe to run again at any time (after a git pull, say): it only adds what is
    missing and never overwrites your config.toml choices unless you pass
    -Cleanup explicitly.

    What it does:
      1. Installs uv (the Python package manager) if it is not on PATH.
      2. uv sync: Python 3.12 and every dependency into .venv (no admin needed).
      3. Detects an NVIDIA GPU. Without one, Whisper runs on the CPU with a
         smaller model.
      4. Detects Ollama and the cleanup model. Without them, cleanup is the
         built-in algorithmic one (no LLM).
      5. Creates config.toml from config.example.toml (first run only).
      6. Downloads the Whisper model once (about 1.6 GB on GPU, 0.5 GB on CPU).
         If huggingface.co is blocked, it gets LocalFlow's own copy from
         GitHub instead (small CPU models only).
      7. Registers LocalFlow to start at login and after sleep. This asks for
         admin once (UAC); see -NoStartup and the README for why.
      8. Starts LocalFlow.

.PARAMETER Cleanup
    auto (default): LLM cleanup if Ollama and the model are present and there
    is an NVIDIA GPU, otherwise algorithmic. Or force "llm" / "rules".

.PARAMETER PullModel
    If Ollama is installed but the cleanup model is not, download it (4.7 GB).

.PARAMETER NoStartup
    Do not register LocalFlow to start at login. Use this when running
    unattended (an AI agent cannot click the UAC prompt).

.PARAMETER NoLaunch
    Do not start LocalFlow at the end.

.PARAMETER WhisperModel
    Whisper model to use, e.g. tiny.en (78 MB), base.en (148 MB), small.en
    (486 MB, the CPU default) or large-v3-turbo (the GPU default). Smaller is
    faster and less accurate.

.PARAMETER ModelZip
    Offline install: a model zip made on another PC with
    `uv run localflow fetch-model --zip <folder>`. Use this when the network
    blocks huggingface.co (corporate proxies such as Zscaler).

.PARAMETER ModelDir
    Offline install: a folder holding a faster-whisper model (model.bin,
    config.json, tokenizer.json, vocabulary.*), e.g. downloaded by hand from
    https://huggingface.co/Systran/faster-whisper-small.en

.PARAMETER HfEndpoint
    Download models from this mirror instead of huggingface.co (an internal
    Artifactory or Nexus proxy). Saved as whisper.hf_endpoint in config.toml.

.PARAMETER Uninstall
    Stop LocalFlow and remove the startup entries. The folder itself is left
    for you to delete.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Cleanup rules -NoStartup

.EXAMPLE
    install.cmd -ModelZip D:\localflow-model-small.en.zip
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'llm', 'rules')]
    [string]$Cleanup = 'auto',
    [switch]$PullModel,
    [switch]$NoStartup,
    [switch]$NoLaunch,
    # Used by update.ps1: no closing banner, no test-load of the speech model.
    [switch]$Quiet,
    # Register the startup tasks again even though they exist (asks for admin).
    [switch]$RegisterAgain,
    [switch]$Uninstall,
    [string]$WhisperModel,
    [string]$ModelZip,
    [string]$ModelDir,
    [string]$HfEndpoint,
    # Internal: the elevated half of the install, run through UAC.
    [switch]$RegisterTasks,
    [switch]$RemoveTasks,
    [string]$UserSid
)

# Not 'Stop': in Windows PowerShell 5.1 that turns any stderr from a native
# command (schtasks, nvidia-smi, uv) into a terminating error. Native commands
# are checked through $LASTEXITCODE instead, and cmdlets that matter pass
# -ErrorAction Stop themselves.
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$model = 'qwen2.5:7b'
$taskNames = @('LocalFlow', 'LocalFlow resume')

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "   $msg" -ForegroundColor Green }
function Note($msg) { Write-Host "   $msg" -ForegroundColor Yellow }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# -- elevated half: scheduled tasks -------------------------------------------

# Why scheduled tasks and not just a Startup shortcut: Windows does not deliver
# keystrokes to a normal program's keyboard hook while an admin window (say, an
# elevated terminal) has focus. Running LocalFlow elevated - which only a
# scheduled task can do at login without a UAC prompt - makes the hotkeys work
# everywhere. The second task restarts it after sleep, which can silently drop
# the keyboard hook.
function Register-LocalFlowTasks([string]$sid) {
    $account = (New-Object Security.Principal.SecurityIdentifier $sid).Translate([Security.Principal.NTAccount]).Value
    $vbs = [Security.SecurityElement]::Escape((Join-Path $root 'restart.vbs'))
    $user = [Security.SecurityElement]::Escape($account)
    $wake = [Security.SecurityElement]::Escape(
        "<QueryList><Query><Select Path='System'>*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]</Select></Query></QueryList>")
    $triggers = @{
        'LocalFlow'        = "<LogonTrigger><UserId>$user</UserId></LogonTrigger>"
        'LocalFlow resume' = "<EventTrigger><Subscription>$wake</Subscription></EventTrigger>"
    }
    foreach ($name in $taskNames) {
        $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>LocalFlow dictation (installed by install.ps1)</Description></RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>$sid</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Triggers>$($triggers[$name])</Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>"$vbs"</Arguments>
    </Exec>
  </Actions>
</Task>
"@
        $file = Join-Path $env:TEMP "localflow-task-$([guid]::NewGuid()).xml"
        [IO.File]::WriteAllText($file, $xml, [Text.Encoding]::Unicode)
        try {
            schtasks /create /tn $name /xml $file /f | Out-Null
            if ($LASTEXITCODE) { throw "schtasks /create failed for '$name'" }
        } finally {
            Remove-Item $file -ErrorAction SilentlyContinue
        }
    }
}

if ($RegisterTasks) {
    try { Register-LocalFlowTasks $UserSid; exit 0 }
    catch { Write-Host $_ -ForegroundColor Red; Start-Sleep 5; exit 1 }
}
if ($RemoveTasks) {
    foreach ($name in $taskNames) { schtasks /delete /tn $name /f 2>$null | Out-Null }
    exit 0
}

# Run the elevated half: directly if we already are admin, else through UAC.
function Invoke-Elevated([string]$arguments) {
    if (Test-Admin) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath @($arguments -split ' ')
        return ($LASTEXITCODE -eq 0)
    }
    $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" $arguments"
    return ($p.ExitCode -eq 0)
}

function Stop-LocalFlow {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'localflow\.cli|restart\.ps1' -and $_.ProcessId -ne $PID } |
        ForEach-Object { taskkill /F /T /PID $_.ProcessId 2>$null | Out-Null }
}

$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

# -- uninstall ------------------------------------------------------------------

if ($Uninstall) {
    Step 'Stopping LocalFlow'
    Stop-LocalFlow
    Ok 'stopped'
    Step 'Removing startup entries'
    $link = Join-Path ([Environment]::GetFolderPath('Startup')) 'LocalFlow.lnk'
    if (Test-Path $link) { Remove-Item $link; Ok "removed $link" }
    schtasks /query /tn 'LocalFlow' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        try {
            if (Invoke-Elevated '-RemoveTasks') { Ok 'removed scheduled tasks' }
            else { Note 'could not remove the scheduled tasks; delete "LocalFlow" and "LocalFlow resume" in Task Scheduler' }
        } catch { Note "UAC declined; delete the 'LocalFlow' tasks in Task Scheduler" }
    }
    Write-Host "`nDone. To remove everything, delete $root"
    Write-Host 'Whisper models stay cached in %USERPROFILE%\.cache\huggingface (delete to reclaim space).'
    exit 0
}

# -- install --------------------------------------------------------------------

if ($env:OS -ne 'Windows_NT') { throw 'LocalFlow is Windows-only.' }
# Resolve a sideload path against where the user ran us, before moving.
foreach ($v in 'ModelZip', 'ModelDir') {
    $val = Get-Variable $v -ValueOnly
    if ($val) {
        if (-not (Test-Path $val)) { throw "-${v}: $val does not exist" }
        Set-Variable $v (Resolve-Path $val).Path
    }
}
Set-Location $root -ErrorAction Stop

Step 'uv (Python package manager)'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Note 'not found; installing from https://astral.sh/uv'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 -ErrorAction Stop | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv installed but not on PATH. Open a new terminal and run install.ps1 again.'
    }
}
Ok (uv --version)

Step 'Python and dependencies (first run downloads ~1.5 GB)'
uv sync --no-dev
if ($LASTEXITCODE) {
    # Usually a TLS-inspecting proxy (Zscaler and the like): its root
    # certificate is in the Windows store, which uv ignores by default.
    Note 'retrying with the Windows certificate store (corporate proxy?)'
    $env:UV_SYSTEM_CERTS = '1'
    uv sync --no-dev
    if ($LASTEXITCODE) { throw 'uv sync failed - see the output above.' }
}
Ok 'environment ready in .venv'

Step 'GPU'
$gpu = $false
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    # Index, don't pipe to Select-Object -First: that stops nvidia-smi early and
    # its exit code reads as a failure.
    $name = @(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null)[0]
    if ($LASTEXITCODE -eq 0 -and $name) { $gpu = $true; Ok "NVIDIA GPU: $name" }
}
if (-not $gpu) { Note 'no NVIDIA GPU: Whisper will run on the CPU with the smaller small.en model' }

Step 'Ollama (optional, for LLM cleanup)'
function Get-OllamaModels {
    # The leading comma keeps an empty list a list (PowerShell unrolls @() to $null).
    try { , @((Invoke-RestMethod http://127.0.0.1:11434/api/tags -TimeoutSec 3 -ErrorAction Stop).models | ForEach-Object { $_.name }) }
    catch { $null }
}
$ollamaInstalled = (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe") -or
    [bool](Get-Command ollama -ErrorAction SilentlyContinue)
$models = Get-OllamaModels
if ($ollamaInstalled -and $null -ne $models -and $models -notcontains $model -and $PullModel) {
    Note "pulling $model (4.7 GB)..."
    ollama pull $model
    $models = Get-OllamaModels
}
$hasModel = $null -ne $models -and $models -contains $model
if ($hasModel) { Ok "Ollama running with $model" }
elseif (-not $ollamaInstalled) { Note 'not installed - fine, the algorithmic cleanup needs no LLM' }
elseif ($null -eq $models) { Note 'installed but not running - start Ollama and re-run to use LLM cleanup' }
else { Note "running, but $model is not pulled. Re-run with -PullModel, or: ollama pull $model" }

$method = switch ($Cleanup) {
    'auto' { if ($hasModel -and $gpu) { 'llm' } else { 'rules' } }
    default { $Cleanup }
}

Step 'Configuration'
$config = Join-Path $root 'config.toml'
$fresh = -not (Test-Path $config)
$settings = @()
if ($fresh) {
    Copy-Item (Join-Path $root 'config.example.toml') $config -ErrorAction Stop
    Ok 'created config.toml from config.example.toml'
    if (-not $gpu) {
        $settings += "('whisper','model','small.en')", "('whisper','device','cpu')", "('whisper','compute_type','int8')"
    }
} else {
    Ok 'config.toml exists; keeping your settings'
}
if ($fresh -or $Cleanup -ne 'auto') { $settings += "('cleanup','method','$method')" }
if ($WhisperModel) { $settings += "('whisper','model','$WhisperModel')" }
if ($HfEndpoint) { $settings += "('whisper','hf_endpoint','$HfEndpoint')" }
if ($settings) {
    $code = "from localflow.config import save_setting as s`n" + (($settings | ForEach-Object { "s$_" }) -join "`n")
    uv run --no-dev python -c $code
    if ($LASTEXITCODE) { throw 'could not write config.toml' }
}
$current = if ((Get-Content $config -Raw) -match '(?m)^\s*method\s*=\s*"rules"') { 'algorithmic (no LLM)' } else { "LLM ($model), algorithmic as fallback" }
Ok "cleanup: $current"

Step 'Whisper model (one-time download)'
$modelReady = $true
$sideload = if ($ModelZip) { $ModelZip } elseif ($ModelDir) { $ModelDir } else { '' }
if ($sideload) {
    uv run --no-dev localflow import-model $sideload
    if ($LASTEXITCODE) { throw "could not import the model from $sideload" }
} else {
    # --fallback-model: a GPU PC behind a block gets small.en (which LocalFlow
    # mirrors on GitHub) rather than no speech model at all.
    uv run --no-dev localflow fetch-model --fallback-model small.en
    if ($LASTEXITCODE -eq 2) {
        # Blocked, not broken: finish the install so only the model is missing.
        $modelReady = $false
        Note 'The speech model could not be downloaded from Hugging Face or from'
        Note "LocalFlow's copy on GitHub (messages above). Everything else is installed."
        Note 'Get the model onto this PC another way, then re-run:'
        Note '  on a PC with internet:  uv run localflow fetch-model --model small.en --zip <folder>'
        Note '  here:                   install.cmd -ModelZip <path to the zip>'
        Note 'Details: "Locked-down networks" in README.md'
    } elseif ($LASTEXITCODE) { throw 'Whisper model download failed - see the output above.' }
}
if ($modelReady) {
    # Small CPU model for the live text on PCs without a GPU, and for
    # always-on listening. Optional: nothing breaks without it.
    uv run --no-dev localflow fetch-model --model base.en 2>&1 | Out-Null
    if ($LASTEXITCODE) { Note 'base.en (live text without a GPU, always-on) not downloaded - it is fetched on first use' }
    else { Ok 'base.en ready (live text without a GPU, always-on listening)' }
}
if ($modelReady -and -not $Quiet) {
    # Load it once, so a broken model (or GPU) shows up now, not at first use.
    # No double quotes in code passed to python -c: Windows PowerShell 5.1
    # strips them from native-command arguments.
    uv run --no-dev python -c @'
from localflow.config import load
from localflow.transcribe import Transcriber
cfg, _ = load()
t = Transcriber(cfg.whisper, cfg.vocabulary)
t.load()
print('   ' + cfg.whisper.model + ' ready on ' + t.device_in_use)
'@
    if ($LASTEXITCODE) { throw 'Whisper model failed to load - see the output above.' }
}

$viaTask = $false
if (-not $NoStartup) {
    Step 'Start at login'
    # Already registered for this folder: keep them, so an update never asks
    # for admin. -RegisterAgain forces it (e.g. after a task settings change).
    $existing = (schtasks /query /tn 'LocalFlow' /xml 2>$null) -join "`n"
    $ours = $LASTEXITCODE -eq 0 -and $existing -like ('*' + (Join-Path $root 'restart.vbs') + '*')
    if ($ours -and -not $RegisterAgain) {
        $viaTask = $true
    } else {
        try {
            $viaTask = Invoke-Elevated "-RegisterTasks -UserSid $sid"
        } catch {
            $viaTask = $false
        }
    }
    if ($viaTask) {
        Ok 'scheduled tasks "LocalFlow" (at login) and "LocalFlow resume" (after sleep), elevated'
        $link = Join-Path ([Environment]::GetFolderPath('Startup')) 'LocalFlow.lnk'
        if (Test-Path $link) { Remove-Item $link }  # the task replaces it
    } else {
        Note 'no admin rights (or UAC declined): using a Startup-folder shortcut instead.'
        Note 'Hotkeys then do not reach admin windows (e.g. an elevated terminal).'
        uv run --no-dev localflow install-startup | Out-Null
        Ok 'Startup shortcut created'
    }
}

if (-not $NoLaunch -and $modelReady) {
    Step 'Starting LocalFlow'
    if ($viaTask) { schtasks /run /tn 'LocalFlow' | Out-Null }
    else { Start-Process wscript.exe -ArgumentList "`"$(Join-Path $root 'restart.vbs')`"" }
    Ok 'started - look for the microphone icon in the system tray (it may be under the ^ arrow)'
}

if ($Quiet) { exit 0 }
Write-Host @"

LocalFlow is installed.

  Caps Lock  or  Ctrl+Win      start / stop dictation (cleaned up)
  Shift+Caps Lock              same, verbatim (no cleanup)
  Esc                          cancel a recording

  Settings: right-click the tray icon, or edit config.toml
  Health check: uv run localflow check
  Log: $root\localflow.log
"@
if (-not $modelReady) {
    Write-Host "`nNOT READY YET: the speech model is missing (see above). To finish," -ForegroundColor Yellow
    Write-Host "run install.cmd -ModelZip <zip> (or -ModelDir <folder>); it then starts LocalFlow." -ForegroundColor Yellow
}
