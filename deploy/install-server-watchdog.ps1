<#
  deploy/install-server-watchdog.ps1 -- register the gpm server watchdog as a Windows
  Scheduled Task that runs deploy\watchdog-server.ps1 every minute.

  The watchdog kills + restarts "python -m gpm server" after FailThreshold consecutive
  health failures (state persists in a state file between the per-minute runs).
  Structure and flags mirror install-agent.ps1.

  !! KEEP THIS FILE PURE ASCII (7-bit, no BOM) !!
  Windows PowerShell 5.1 reads a BOM-less file with the ANSI code page (GB2312 on
  zh-CN hosts); non-ASCII bytes there can swallow line breaks and break the script.

  Examples (run from an elevated PowerShell for a SYSTEM task):
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-server-watchdog.ps1 -DryRun
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-server-watchdog.ps1 -HealthUrl http://127.0.0.1:8620/api/health
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-server-watchdog.ps1 -Status
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-server-watchdog.ps1 -Stop
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-server-watchdog.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$HealthUrl = 'http://127.0.0.1:8620/api/health',
    [int]$FailThreshold = 3,
    [string]$SourceDir = '',
    [string]$RestartCommand = 'python -m gpm server',
    [string]$TaskName = 'gpm-server-watchdog',
    [switch]$Help,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$Start,
    [switch]$Stop,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$WatchdogScript = 'watchdog-server.ps1'

function Say([string]$m) { Write-Host ('[gpm] ' + $m) }
function Warn([string]$m) { Write-Host ('[gpm][warn] ' + $m) -ForegroundColor Yellow }
function Die([string]$m) { Write-Host ('[gpm][error] ' + $m) -ForegroundColor Red; exit 1 }

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
if (-not $SourceDir) { $SourceDir = (Resolve-Path -LiteralPath (Join-Path $ScriptDir '..')).Path }
$WatchdogPath = Join-Path $ScriptDir $WatchdogScript

$IsAdmin = $false
try {
    $winId = [Security.Principal.WindowsIdentity]::GetCurrent()
    $winPr = New-Object Security.Principal.WindowsPrincipal($winId)
    $IsAdmin = $winPr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { $IsAdmin = $false }

function Get-GpmTask {
    try { return (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop) } catch { return $null }
}

function New-TaskArguments {
    # Arguments handed to powershell.exe: the watchdog script plus every tunable.
    $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $WatchdogPath + '"'),
           '-HealthUrl', ('"' + $HealthUrl + '"'),
           '-FailThreshold', ('' + $FailThreshold),
           '-SourceDir', ('"' + $SourceDir + '"'),
           '-RestartCommand', ('"' + $RestartCommand + '"'))
    return ($a -join ' ')
}

function Show-Status {
    Say ('task name      : ' + $TaskName)
    Say ('watchdog script: ' + $WatchdogPath)
    Say ('health url     : ' + $HealthUrl)
    Say ('fail threshold : ' + $FailThreshold)
    Say ('restart command: ' + $RestartCommand)
    $t = Get-GpmTask
    if (-not $t) {
        Say 'task state     : NOT REGISTERED'
        return
    }
    Say ('task state     : ' + $t.State)
    try {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
        Say ('last run time  : ' + $info.LastRunTime)
        Say ('last result    : ' + $info.LastTaskResult + '  (0 = success, 267009 = still running)')
        Say ('next run time  : ' + $info.NextRunTime)
    } catch { }
    foreach ($a in $t.Actions) { Say ('action         : ' + $a.Execute + ' ' + $a.Arguments) }
    # tail of the watchdog log (written by watchdog-server.ps1 under the state dir)
    $log = Join-Path (Join-Path $env:ProgramData 'gpm-server-watchdog') 'watchdog.log'
    if (-not (Test-Path -LiteralPath $log)) {
        $log = Join-Path (Join-Path $env:LOCALAPPDATA 'gpm-server-watchdog') 'watchdog.log'
    }
    if (Test-Path -LiteralPath $log) {
        Say ('  --- last 10 lines of ' + $log)
        try { Get-Content -LiteralPath $log -Tail 10 -ErrorAction SilentlyContinue | ForEach-Object { Say ('  | ' + $_) } } catch { }
    } else {
        Say '  (no watchdog log yet)'
    }
}

# ---------------------------------------------------------------- main
function Show-Usage {
    Write-Host ''
    Write-Host 'gpm server watchdog installer for Windows (Scheduled Task, every 1 minute)'
    Write-Host ''
    Write-Host '  (no flags)          install + start the watchdog task with the options below'
    Write-Host '  -HealthUrl URL      health endpoint to poll                  (default: http://127.0.0.1:8620/api/health)'
    Write-Host '  -FailThreshold N    consecutive failures before a restart    (default: 3)'
    Write-Host '  -SourceDir DIR      gpm source checkout (contains src\gpm)   (default: parent of deploy\)'
    Write-Host '  -RestartCommand CMD command used to start the server         (default: python -m gpm server)'
    Write-Host '  -TaskName NAME      scheduled task name                      (default: gpm-server-watchdog)'
    Write-Host '  -DryRun             print every planned action, change nothing'
    Write-Host '  -Status             print task state + tail of the watchdog log'
    Write-Host '  -Start / -Stop      enable+start / stop+disable the task'
    Write-Host '  -Uninstall          stop and remove the task (idempotent)'
    Write-Host '  -Help               show this help'
    Write-Host ''
    Write-Host 'Example:'
    Write-Host '  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-server-watchdog.ps1'
    Write-Host ''
}

if ($Help) { Show-Usage; exit 0 }

Say ('elevated      : ' + $IsAdmin)
Say ('source dir    : ' + $SourceDir)

if ($Uninstall) {
    Say ('uninstall task: ' + $TaskName)
    $t = Get-GpmTask
    if ($t) {
        if ($DryRun) {
            Say ('[DRY-RUN] would stop and unregister task ' + $TaskName)
        } else {
            if ($t.State -eq 'Running') {
                try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop ; Say 'task stopped' } catch { Warn ('stop failed: ' + $_.Exception.Message) }
            }
            try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop ; Say 'task unregistered' } catch { Warn ('unregister failed: ' + $_.Exception.Message) }
        }
    } else {
        Say ('task ' + $TaskName + ' is not registered (nothing to remove)')
    }
    Say 'watchdog state/log files under the state dir are kept on purpose.'
    if ($Status) { Show-Status }
    exit 0
}

if ($Stop) {
    $t = Get-GpmTask
    if (-not $t) { Say ('task ' + $TaskName + ' is not registered') }
    elseif ($DryRun) { Say ('[DRY-RUN] would stop and disable task ' + $TaskName) }
    else {
        try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop ; Say ('task stopped: ' + $TaskName) } catch { Warn ('stop failed: ' + $_.Exception.Message) }
        try { Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null ; Say 'task disabled (use -Start to re-enable)' } catch { Warn ('disable failed: ' + $_.Exception.Message) }
    }
    if ($Status) { Show-Status }
    exit 0
}

if ($Start) {
    $t = Get-GpmTask
    if (-not $t) { Die ('task ' + $TaskName + ' is not registered; install it first') }
    if ($DryRun) { Say ('[DRY-RUN] would enable and start task ' + $TaskName) }
    else {
        try { Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null ; Say 'task enabled' } catch { Warn ('enable failed: ' + $_.Exception.Message) }
        try { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop ; Say ('task started: ' + $TaskName) } catch { Warn ('start failed: ' + $_.Exception.Message) }
    }
    if ($Status) { Show-Status }
    exit 0
}

# ---- status only (standalone -Status; without this branch it would fall through to install)
if ($Status) {
    Show-Status
    exit 0
}

# ---------------------------------------------------------------- install
if (-not (Test-Path -LiteralPath $WatchdogPath)) {
    Die ('watchdog script not found next to the installer: ' + $WatchdogPath)
}
$srcMain = Join-Path $SourceDir 'src\gpm\__main__.py'
if (-not (Test-Path -LiteralPath $srcMain)) {
    Warn ('-SourceDir does not look like a gpm checkout (missing src\gpm\__main__.py): ' + $SourceDir)
}

$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action = New-ScheduledTaskAction -Execute $psExe -Argument (New-TaskArguments) -WorkingDirectory $ScriptDir
# Every minute, forever: -RepetitionDuration omitted on purpose (Task Scheduler reads
# the missing <Duration> as "indefinitely"; bounded spans like [TimeSpan]::MaxValue are
# rejected as out of range).
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)

if ($DryRun) {
    Say ''
    Say '[DRY-RUN] no task will be changed. Planned actions:'
    Say ('  1. action    : ' + $psExe)
    Say ('     arguments : ' + (New-TaskArguments))
    Say ('  2. trigger   : once, then repeat every 1 minute (indefinitely)')
    Say ('  3. principal : ' + $(if ($IsAdmin) { 'SYSTEM / Highest' } else { ($env:USERDOMAIN + '\' + $env:USERNAME) + ' / Interactive (log on required)' }))
    Say '  4. start the task immediately'
    Say ''
    Say '[DRY-RUN] done.'
    if ($Status) { Show-Status }
    exit 0
}

if ($IsAdmin) {
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
} else {
    Warn 'not elevated: the watchdog will only run while this user is logged on (run as Administrator for a SYSTEM task)'
    $principal = New-ScheduledTaskPrincipal -UserId ($env:USERDOMAIN + '\' + $env:USERNAME) -LogonType Interactive -RunLevel Limited
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description ('gpm server health watchdog -> ' + $HealthUrl) -Force | Out-Null
Say ('task registered: ' + $TaskName)
Enable-ScheduledTask -TaskName $TaskName | Out-Null
Start-ScheduledTask -TaskName $TaskName
Say ('task started: ' + $TaskName)
Say ('verify with: powershell -NoProfile -ExecutionPolicy Bypass -File ' + (Join-Path $ScriptDir 'install-server-watchdog.ps1') + ' -Status')
if ($Status) { Show-Status }
exit 0
