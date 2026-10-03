<#
  deploy/watchdog-server.ps1 -- gpm server health watchdog for Windows.

  Designed to be run by Task Scheduler every minute (see install-server-watchdog.ps1),
  or continuously with -Loop. Each check:
    * GET /api/health (no DB/lock on the server side, so a stuck thread pool still answers);
    * on success: reset the consecutive-failure counter (persisted in a state file);
    * on failure: increment the counter; after FailThreshold consecutive failures:
        - kill leftover "python.exe -m gpm server" processes (WMI CommandLine filter,
          same idea as kill-gpm-server.ps1 -- never matches this PowerShell process);
        - start the configured restart command (default: python -m gpm server) with
          PYTHONPATH=<SourceDir>\src so a source checkout works without pip install.

  !! KEEP THIS FILE PURE ASCII (7-bit, no BOM) !!
  Windows PowerShell 5.1 reads a BOM-less file with the ANSI code page (GB2312 on
  zh-CN hosts); non-ASCII bytes there can swallow line breaks and break the script.

  Examples:
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\watchdog-server.ps1 -DryRun
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\watchdog-server.ps1 -Loop
#>
[CmdletBinding()]
param(
    [string]$HealthUrl = 'http://127.0.0.1:8620/api/health',
    [int]$TimeoutSec = 5,
    [int]$FailThreshold = 3,
    [string]$SourceDir = '',
    [string]$RestartCommand = 'python -m gpm server',
    [string]$StateDir = '',
    [int]$RestartCooldownSec = 300,
    [switch]$Loop,
    [int]$LoopIntervalSeconds = 60,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Say([string]$m) { Write-Host ('[gpm-watchdog] ' + $m) }
function Warn([string]$m) { Write-Host ('[gpm-watchdog][warn] ' + $m) -ForegroundColor Yellow }

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
if (-not $SourceDir) { $SourceDir = (Resolve-Path -LiteralPath (Join-Path $ScriptDir '..')).Path }
if (-not $StateDir) {
    $IsAdmin = $false
    try {
        $winId = [Security.Principal.WindowsIdentity]::GetCurrent()
        $winPr = New-Object Security.Principal.WindowsPrincipal($winId)
        $IsAdmin = $winPr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { $IsAdmin = $false }
    $StateDir = if ($IsAdmin) { Join-Path $env:ProgramData 'gpm-server-watchdog' } else { Join-Path $env:LOCALAPPDATA 'gpm-server-watchdog' }
}

$StateFile = Join-Path $StateDir 'state.json'
$LogFile = Join-Path $StateDir 'watchdog.log'

function Log([string]$m) {
    $line = ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m)
    Say $m
    try {
        if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Force -Path $StateDir | Out-Null }
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch { }
}

function Read-State {
    try {
        $raw = Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        return [pscustomobject]@{
            fail_count   = [int]($raw.fail_count)
            last_restart = [string]($raw.last_restart)
            last_ok      = [string]($raw.last_ok)
            restarts     = [int]($raw.restarts)
        }
    } catch {
        return [pscustomobject]@{ fail_count = 0; last_restart = ''; last_ok = ''; restarts = 0 }
    }
}

function Write-State($st) {
    try {
        if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Force -Path $StateDir | Out-Null }
        $json = $st | ConvertTo-Json -Depth 3
        # UTF-8 without BOM, mirroring install-agent.ps1's Write-TextNoBom.
        $enc = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($StateFile, $json, $enc)
    } catch { Warn ('cannot write state file: ' + $_.Exception.Message) }
}

function Test-Health {
    try {
        $resp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500)
    } catch {
        return $false
    }
}

function Stop-OldServer {
    # Match ONLY the python interpreter running "-m gpm server" (same WMI CommandLine
    # filter as kill-gpm-server.ps1), never the PowerShell host running this watchdog.
    $pids = @()
    try {
        $procs = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and ($_.CommandLine -match '-m\s+gpm\s+server') })
        $pids = @($procs | Select-Object -ExpandProperty ProcessId -Unique)
    } catch { Warn ('process scan failed: ' + $_.Exception.Message) }
    foreach ($id in $pids) {
        if ($DryRun) { Say ('[DRY-RUN] would stop old server process pid=' + $id); continue }
        Say ('stopping old server process pid=' + $id)
        try { Stop-Process -Id $id -Force -ErrorAction Stop } catch { Warn ('cannot stop pid ' + $id) }
    }
    return $pids.Count
}

function Start-Server {
    $parts = $RestartCommand.Trim() -split '\s+', 2
    $exe = $parts[0]
    $argStr = if ($parts.Count -gt 1) { $parts[1] } else { '' }
    $srcFull = ''
    try { $srcFull = (Resolve-Path -LiteralPath $SourceDir).Path } catch { }
    if ($DryRun) {
        Say ('[DRY-RUN] would start: ' + $exe + ' ' + $argStr + '  (workdir=' + $srcFull + ', PYTHONPATH=' + (Join-Path $srcFull 'src') + ')')
        return
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = $argStr
    $psi.WorkingDirectory = if ($srcFull) { $srcFull } else { (Get-Location).Path }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables['PYTHONPATH'] = Join-Path $srcFull 'src'
    try {
        [void][System.Diagnostics.Process]::Start($psi)
        Say ('server restarted: ' + $RestartCommand)
    } catch {
        Warn ('restart failed: ' + $_.Exception.Message)
    }
}

function Invoke-Check {
    $st = Read-State
    if (Test-Health) {
        $st.fail_count = 0
        $st.last_ok = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        Say ('health OK: ' + $HealthUrl + ' (fail_count=0)')
        if (-not $DryRun) { Write-State $st }
        return $true
    }

    $st.fail_count = $st.fail_count + 1
    Say ('health FAIL (' + $st.fail_count + '/' + $FailThreshold + '): ' + $HealthUrl)

    # Cooldown: another watchdog run (or the previous minute's run) may have restarted already.
    if ($st.last_restart) {
        try {
            $ago = ((Get-Date) - [datetime]::Parse($st.last_restart)).TotalSeconds
            if ($ago -ge 0 -and $ago -lt $RestartCooldownSec) {
                Say ('restart was ' + [int]$ago + 's ago (cooldown ' + $RestartCooldownSec + 's), skip this round')
                if (-not $DryRun) { Write-State $st }
                return $false
            }
        } catch { }
    }

    if ($st.fail_count -lt $FailThreshold) {
        if (-not $DryRun) { Write-State $st }
        return $false
    }

    Say ('threshold reached (' + $st.fail_count + ' consecutive failures), restarting server')
    if ($DryRun) {
        Say '[DRY-RUN] would kill old python -m gpm server processes and start a fresh one'
        Stop-OldServer | Out-Null
        Start-Server
        return $false
    }
    [void](Stop-OldServer)
    Start-Server
    $st.fail_count = 0
    $st.restarts = $st.restarts + 1
    $st.last_restart = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    Write-State $st
    return $false
}

# ---------------------------------------------------------------- main
if ($DryRun) { Say '[DRY-RUN] checking health, no process will be touched' }

if ($Loop) {
    Say ('loop mode: every ' + $LoopIntervalSeconds + 's, threshold=' + $FailThreshold + ', url=' + $HealthUrl)
    while ($true) {
        [void](Invoke-Check)
        Start-Sleep -Seconds $LoopIntervalSeconds
    }
} else {
    # Single-shot mode: one check per invocation (Task Scheduler runs us every minute;
    # the failure counter survives in the state file between runs).
    [void](Invoke-Check)
}
