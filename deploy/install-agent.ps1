<#
  deploy/install-agent.ps1 -- gpm probe agent installer for Windows.

  Runs the agent as a Windows Scheduled Task (zero extra dependencies:
  no pywin32, no NSSM, no service wrapper):
    * trigger  : At startup (plus At logon when the shell is not elevated)
    * restart  : Task Scheduler restart-on-failure (RestartCount / RestartInterval)
    * launcher : <DataDir>\gpm-agent-run.cmd sets PYTHONPATH=<repo>\src and runs
                 "python -m gpm --config <DataDir>\agent.json agent --server ...",
                 with stdout/stderr redirected to log files.
                 NOTE: --config is a GLOBAL flag in gpm's argparse, it must come
                 BEFORE the "agent" subcommand, otherwise argparse exits with 2.

  !! KEEP THIS FILE PURE ASCII (7-bit, no BOM) !!
  Windows PowerShell 5.1 reads a BOM-less file with the ANSI code page (GB2312 on
  zh-CN hosts); non-ASCII bytes there can swallow line breaks and silently break
  the whole script. Chinese documentation lives in .docs/DEPLOY.md (local only, not in the repo).

  Examples (run from an elevated PowerShell for a real boot-time service):
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-agent.ps1 -ServerUrl http://10.0.0.5:8620 -Token CHANGE_ME -Name win-node-01
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-agent.ps1 -Status
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-agent.ps1 -Uninstall
    powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-agent.ps1 -DryRun -ServerUrl http://10.0.0.5:8620 -Token CHANGE_ME
#>
[CmdletBinding()]
param(
    [string]$ServerUrl = '',
    [string]$Token = '',
    [string]$Name = '',
    [string]$PythonExe = '',
    [string]$SourceDir = '',
    [string]$DataDir = '',
    [string]$Tags = '{}',
    [string]$TaskName = 'gpm-agent',
    [int]$WatchdogMinutes = 2,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$Start,
    [switch]$Stop,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$TailLines = 20
$LauncherName = 'gpm-agent-run.cmd'
$ConfigName = 'agent.json'

function Say([string]$m) { Write-Host ('[gpm] ' + $m) }
function Warn([string]$m) { Write-Host ('[gpm][warn] ' + $m) -ForegroundColor Yellow }
function Die([string]$m) { Write-Host ('[gpm][error] ' + $m) -ForegroundColor Red; exit 1 }
function NewLineChar { return ([string][char]13 + [string][char]10) }

# ---------------------------------------------------------------- environment
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
if (-not $SourceDir) { $SourceDir = (Resolve-Path -LiteralPath (Join-Path $ScriptDir '..')).Path }

$IsAdmin = $false
try {
    $winId = [Security.Principal.WindowsIdentity]::GetCurrent()
    $winPr = New-Object Security.Principal.WindowsPrincipal($winId)
    $IsAdmin = $winPr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { $IsAdmin = $false }

$DefaultDataDir = if ($IsAdmin) {
    Join-Path $env:ProgramData 'gpm-agent'
} else {
    Join-Path $env:LOCALAPPDATA 'gpm-agent'
}

function Get-ConfiguredDataDir {
    if ($DataDir) { return $DataDir }
    $cfg = Join-Path $DefaultDataDir $ConfigName
    if (Test-Path -LiteralPath $cfg) {
        try {
            $j = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($j.agent -and $j.agent.data_dir) { return [string]$j.agent.data_dir }
        } catch { }
    }
    return $DefaultDataDir
}

function Write-TextNoBom([string]$Path, [string]$Text) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $parent = [System.IO.Path]::GetDirectoryName($full)
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    # UTF-8 without BOM: gpm reads JSON with encoding="utf-8", a BOM would break json.loads.
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($full, $Text, $enc)
}

# ---------------------------------------------------------------- python detect
function Test-Py {
    param([string]$Exe, [string[]]$Extra = @())
    if (-not $Exe) { return '' }
    $c = Get-Command $Exe -ErrorAction SilentlyContinue
    if (-not $c) { return '' }
    $p = $c.Source
    if (-not $p) { $p = $c.Path }
    if (-not $p) { return '' }
    # skip the Microsoft Store execution alias (it opens the Store page instead of running)
    if ($p -like '*\WindowsApps\*') { return '' }
    $out = @()
    try {
        $out = & $p @Extra -c 'import sys; print(sys.executable) if sys.version_info>=(3,11) else print("TOOOLD")' 2>$null
    } catch { return '' }
    if ($LASTEXITCODE -ne 0) { return '' }
    $line = (@($out) -join '').Trim()
    if (-not $line -or $line -eq 'TOOOLD') { return '' }
    if ($line -like '*\WindowsApps\*') { return '' }
    if (-not (Test-Path -LiteralPath $line)) { return '' }
    return $line
}

function Resolve-PythonExe {
    if ($PythonExe) {
        $r = Test-Py $PythonExe
        if (-not $r) { Die ('-PythonExe is not a usable Python >= 3.11 (or is a Store alias): ' + $PythonExe) }
        return $r
    }
    if ($env:GPM_PYTHON) {
        $r = Test-Py $env:GPM_PYTHON
        if ($r) { return $r }
        Warn ('GPM_PYTHON is not usable: ' + $env:GPM_PYTHON)
    }
    foreach ($v in @('3.13', '3.12', '3.11', '3')) {
        $r = Test-Py 'py' @('-' + $v)
        if ($r) { return $r }
    }
    foreach ($n in @('python', 'python3')) {
        $r = Test-Py $n
        if ($r) { return $r }
    }
    $wellKnown = @()
    foreach ($base in @($env:ProgramData, $env:LOCALAPPDATA)) {
        if (-not $base) { continue }
        $wellKnown += (Join-Path $base 'miniconda3\python.exe')
        $wellKnown += (Join-Path $base 'anaconda3\python.exe')
        foreach ($v in @('313', '312', '311')) {
            $wellKnown += (Join-Path $base ('Programs\Python\Python' + $v + '\python.exe'))
        }
    }
    foreach ($v in @('313', '312', '311')) { $wellKnown += ('C:\Python' + $v + '\python.exe') }
    foreach ($e in $wellKnown) {
        $r = Test-Py $e
        if ($r) { return $r }
    }
    return ''
}

# ---------------------------------------------------------------- artifacts
function Convert-Tags([string]$Raw) {
    $raw = ('' + $Raw).Trim()
    if (-not $raw) { return [pscustomobject]@{} }
    if (Test-Path -LiteralPath $raw) { $raw = (Get-Content -LiteralPath $raw -Raw -Encoding UTF8) }
    try { return ($raw | ConvertFrom-Json) } catch { Die ('-Tags is not valid JSON: ' + $Raw + ' (hint: when launching powershell.exe from PowerShell, escape the inner quotes: -Tags ''{\"region\":\"cn-hz\"}''' ) }
}

function New-AgentConfigJson {
    param([string]$Server, [string]$Tok, [string]$NodeName, $TagsObj, [string]$Dir)
    $cfg = [ordered]@{
        agent = [ordered]@{
            server_url       = $Server
            register_token   = $Tok
            name             = $NodeName
            tags             = $TagsObj
            data_dir         = $Dir
            heartbeat_interval = 15
            poll_interval    = 10
            report_interval  = 5
        }
        logging = [ordered]@{ level = 'INFO'; file = (Join-Path $Dir 'agent.log') }
    }
    return ($cfg | ConvertTo-Json -Depth 6)
}

function New-LauncherText {
    param([string]$Py, [string]$Src, [string]$Cfg, [string]$Server, [string]$Tok, [string]$NodeName, [string]$Dir)
    # The :gpm_agent_loop below IS the crash supervisor: python is relaunched a few
    # seconds after it exits, forever. Task Scheduler then only sees one long-running
    # task (MultipleInstances=IgnoreNew) and RestartOnFailure covers the launcher
    # itself dying. "ping -n" is used as a dependency-free sleep: "timeout" aborts
    # with "input redirection is not supported" when the task has no console.
    $lines = @(
        '@echo off',
        'rem gpm probe agent launcher - generated by deploy/install-agent.ps1',
        'rem Written with the ANSI code page on purpose: cmd.exe reads .cmd bytes that way.',
        'setlocal enableextensions',
        ('set "PYTHONPATH=' + $Src + '"'),
        'set "PYTHONIOENCODING=utf-8"',
        'set "PYTHONUTF8=1"',
        ('cd /d "' + $Dir + '"'),
        ':gpm_agent_loop',
        ('"' + $Py + '" -m gpm --config "' + $Cfg + '" agent --server "' + $Server + '" --token "' + $Tok + '" --name "' + $NodeName + '" >> "' + (Join-Path $Dir 'agent.out.log') + '" 2>> "' + (Join-Path $Dir 'agent.err.log') + '"'),
        'set "GPM_EXIT=%ERRORLEVEL%"',
        ('echo [%DATE% %TIME%] gpm agent exited with code %GPM_EXIT% - restarting >> "' + (Join-Path $Dir 'agent.err.log') + '"'),
        'rem about 5 seconds of sleep without depending on any extra binary',
        'ping -n 6 127.0.0.1 >nul 2>&1',
        'goto gpm_agent_loop'
    )
    $nl = NewLineChar
    return (($lines -join $nl) + $nl)
}

function Get-GpmTask {
    try { return (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop) } catch { return $null }
}

function Register-GpmTask {
    param([string]$Launcher, [string]$Desc)
    $cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
    $action = New-ScheduledTaskAction -Execute $cmdExe -Argument ('/c "' + $Launcher + '"') -WorkingDirectory (Split-Path -Parent $Launcher)
    $triggers = @(New-ScheduledTaskTrigger -AtStartup)
    # Two independent supervisors keep the agent alive:
    #  a) RestartOnFailure (Count/Interval) - used when the launcher exits non-zero;
    #     Task Scheduler skips it for instances started by hand (Start-ScheduledTask),
    #     which is exactly what the self-test does, so b) exists too.
    #  b) Repetition on the startup trigger = watchdog. "IgnoreNew" throws the extra
    #     start away while the agent is healthy, so this behaves as a periodic
    #     "is it still alive?" check and brings a dead agent back within $WatchdogMinutes.
    if ($WatchdogMinutes -gt 0) {
        # Do NOT pass -RepetitionDuration: the only accepted values are bounded spans
        # ([TimeSpan]::MaxValue is rejected as out of range) and omitting it makes the
        # XML carry no <Duration> at all, which Task Scheduler reads as "indefinitely".
        $rep = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes)).Repetition
        $triggers[0].Repetition = $rep
    }
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
    if ($IsAdmin) {
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    } else {
        Warn 'not elevated: task will run only while this user is logged on (run as Administrator for boot-time start as SYSTEM)'
        $principal = New-ScheduledTaskPrincipal -UserId ($env:USERDOMAIN + '\' + $env:USERNAME) -LogonType Interactive -RunLevel Limited
        $triggers += (New-ScheduledTaskTrigger -AtLogOn)
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description $Desc -Force | Out-Null
}

function Stop-LeftoverAgent([string]$Dir, [bool]$Dry) {
    # Leftovers = python started with OUR config file, plus the cmd.exe host that runs
    # the launcher loop (killing python alone would just make the loop relaunch it).
    # Only processes whose command line carries our exact config path are matched:
    # matching a bare path substring once killed unrelated shells, so do not widen it.
    $cfg = (Join-Path $Dir $ConfigName).ToLower()
    $pids = @()
    try {
        $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue | Where-Object {
            $_.ProcessId -ne $PID -and $_.CommandLine -and
            ($_.CommandLine.ToLower().IndexOf($cfg) -ge 0)
        })
        $pids += $procs.ProcessId
        $parents = @($procs | Select-Object -ExpandProperty ParentProcessId -Unique)
        if ($parents.Count -gt 0) {
            $hosts = @(Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue | Where-Object {
                $parents -contains $_.ProcessId
            })
            $pids += $hosts.ProcessId
        }
    } catch { }
    foreach ($id in ($pids | Where-Object { $_ } | Select-Object -Unique)) {
        if ($Dry) { Say ('[DRY-RUN] would stop leftover agent process pid=' + $id) ; continue }
        Say ('stopping leftover agent process pid=' + $id)
        try { Stop-Process -Id $id -Force -ErrorAction Stop } catch { Warn ('cannot stop pid ' + $id) }
    }
}

function Show-LogTail([string]$Path, [int]$N) {
    if (-not (Test-Path -LiteralPath $Path)) { Say ('  (no log file yet: ' + $Path + ')') ; return }
    Say ('  --- last ' + $N + ' lines of ' + $Path)
    try {
        Get-Content -LiteralPath $Path -Tail $N -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Say ('  | ' + $_) }
    } catch { Warn ('cannot read ' + $Path) }
}

function Show-Status {
    $dir = Get-ConfiguredDataDir
    Say ('task name     : ' + $TaskName)
    Say ('data dir      : ' + $dir)
    $t = Get-GpmTask
    if (-not $t) {
        Say 'task state    : NOT REGISTERED'
        Show-LogTail (Join-Path $dir 'agent.log') $TailLines
        return
    }
    $info = $null
    try { $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop } catch { }
    Say ('task state    : ' + $t.State)
    if ($info) {
        Say ('last run time : ' + $info.LastRunTime)
        Say ('last result   : ' + $info.LastTaskResult + '  (0 = success, 267009 = still running, 1/2 = failed)')
        Say ('next run time : ' + $info.NextRunTime)
        Say ('missed runs   : ' + $info.NumberOfMissedRuns)
    }
    $s = $t.Settings
    Say ('restart count : ' + $s.RestartCount + ' every ' + $s.RestartInterval)
    Say ('time limit    : ' + $s.ExecutionTimeLimit + '  (PT0S = unlimited)')
    Say ('run as        : ' + $t.Principal.UserId + ' / ' + $t.Principal.RunLevel)
    foreach ($a in $t.Actions) { Say ('action        : ' + $a.Execute + ' ' + $a.Arguments) }
    foreach ($tr in $t.Triggers) { Say ('trigger       : ' + $tr.CimClass.CimClassName) }
    Show-LogTail (Join-Path $dir 'agent.log') $TailLines
    Show-LogTail (Join-Path $dir 'agent.err.log') 10
}

# ---------------------------------------------------------------- main
if (-not $ServerUrl -and -not $Token -and -not $Status -and -not $Uninstall -and -not $Start -and -not $Stop) {
    Write-Host ''
    Write-Host 'gpm agent installer for Windows (Scheduled Task, no extra dependencies)'
    Write-Host ''
    Write-Host '  -ServerUrl URL   server base URL, e.g. http://10.0.0.5:8620   (required to install)'
    Write-Host '  -Token TOKEN     register token from the server config       (required to install)'
    Write-Host '  -Name NAME       node name shown in the server UI            (default: %COMPUTERNAME%)'
    Write-Host '  -PythonExe PATH  python.exe to use                           (default: auto-detect >= 3.11)'
    Write-Host '  -SourceDir DIR   gpm source checkout (contains src\gpm)      (default: parent of deploy\)'
    Write-Host '  -DataDir DIR     data/log/launcher directory'
    Write-Host '  -Tags JSON       node tags, e.g. {"region":"cn-hz"}          (default: {})'
    Write-Host '  -TaskName NAME   scheduled task name                         (default: gpm-agent)'
    Write-Host '  -WatchdogMinutes N  repeating liveness check on the startup trigger (default: 2, 0 = off)'
    Write-Host '  -DryRun          print every planned action, change nothing'
    Write-Host '  -Status          print task state + tail of the agent logs'
    Write-Host '  -Start / -Stop   start / stop the scheduled task'
    Write-Host '  -Uninstall       stop and remove the task (idempotent)'
    Write-Host ''
    Write-Host 'Example:'
    Write-Host '  powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-agent.ps1 -ServerUrl http://10.0.0.5:8620 -Token CHANGE_ME'
    Write-Host ''
    exit 1
}

Say ('elevated      : ' + $IsAdmin)
Say ('source dir    : ' + $SourceDir)

if ($Uninstall) {
    $dir = Get-ConfiguredDataDir
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
    Stop-LeftoverAgent $dir $DryRun.IsPresent
    $lf = Join-Path $dir $LauncherName
    if (Test-Path -LiteralPath $lf) {
        if ($DryRun) { Say ('[DRY-RUN] would remove launcher ' + $lf) }
        else { Remove-Item -LiteralPath $lf -Force ; Say ('launcher removed: ' + $lf) }
    }
    Say 'config file and logs under the data dir are kept on purpose.'
    if ($Status) { Show-Status }
    exit 0
}

if ($Stop) {
    $t = Get-GpmTask
    if (-not $t) { Say ('task ' + $TaskName + ' is not registered') }
    elseif ($DryRun) { Say ('[DRY-RUN] would stop task ' + $TaskName) }
    else {
        try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop ; Say ('task stopped: ' + $TaskName) } catch { Warn ('stop failed: ' + $_.Exception.Message) }
        # Disable as well, otherwise the repetition watchdog starts the agent again.
        try { Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null ; Say 'task disabled (use -Start to re-enable)' } catch { Warn ('disable failed: ' + $_.Exception.Message) }
        Stop-LeftoverAgent (Get-ConfiguredDataDir) $false
    }
    if ($Status) { Show-Status }
    exit 0
}

if ($Start -and -not $ServerUrl -and -not $Token) {
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

# ---- status only (no action flags, no install parameters) ----
# Without this branch a bare "-Status" fell through to the install path and died
# with "-ServerUrl is required", although the header documents -Status as standalone.
if ($Status -and -not $ServerUrl -and -not $Token) {
    Show-Status
    exit 0
}

# ---- install ----
if (-not $ServerUrl) { Die '-ServerUrl is required to install (see -Status / -Uninstall for other modes)' }
if (-not $Token) { Die '-Token is required to install' }
$ServerUrl = $ServerUrl.TrimEnd('/')

$srcFull = ''
try { $srcFull = (Resolve-Path -LiteralPath $SourceDir).Path } catch { Die ('-SourceDir does not exist: ' + $SourceDir) }
$pkgMain = Join-Path $srcFull 'src\gpm\__main__.py'
if (-not (Test-Path -LiteralPath $pkgMain)) { Die ('-SourceDir is not a gpm source checkout (missing src\gpm\__main__.py): ' + $srcFull) }

$PyFull = Resolve-PythonExe
if (-not $PyFull) { Die 'no Python >= 3.11 found; install it or pass -PythonExe <path to python.exe>' }
if ($PythonExe) { Say ('python        : ' + $PyFull + '  (from -PythonExe)') } else { Say ('python        : ' + $PyFull + '  (auto-detected)') }

$NodeName = if ($Name) { $Name } else { $env:COMPUTERNAME }
$DirFull = Get-ConfiguredDataDir
$TagsObj = Convert-Tags $Tags
$cfgPath = Join-Path $DirFull $ConfigName
$launcherPath = Join-Path $DirFull $LauncherName
$cfgJson = New-AgentConfigJson -Server $ServerUrl -Tok $Token -NodeName $NodeName -TagsObj $TagsObj -Dir $DirFull
$launcherText = New-LauncherText -Py $PyFull -Src (Join-Path $srcFull 'src') -Cfg $cfgPath -Server $ServerUrl -Tok $Token -NodeName $NodeName -Dir $DirFull

Say ('node name     : ' + $NodeName)
Say ('data dir      : ' + $DirFull)
Say ('config file   : ' + $cfgPath)
Say ('launcher      : ' + $launcherPath)

if ($DryRun) {
    $nl = NewLineChar
    Say ''
    Say '[DRY-RUN] no file or task will be changed. Planned actions:'
    Say ('  1. create directory   : ' + $DirFull)
    Say ('  2. write config file  : ' + $cfgPath)
    Write-Host '----- agent.json -----'
    Write-Host $cfgJson
    Write-Host '----------------------'
    Say ('  3. write launcher     : ' + $launcherPath)
    Write-Host '----- gpm-agent-run.cmd -----'
    Write-Host ($launcherText -replace $nl, $nl)
    Write-Host '-----------------------------'
    Say ('  4. register scheduled task: ' + $TaskName)
    Say ('     action  : ' + (Join-Path $env:SystemRoot 'System32\cmd.exe') + ' /c "' + $launcherPath + '"')
    Say ('     trigger : AtStartup' + $(if ($IsAdmin) { '' } else { ' + AtLogOn' }))
    Say ('     principal: ' + $(if ($IsAdmin) { 'SYSTEM / Highest' } else { ($env:USERDOMAIN + '\' + $env:USERNAME) + ' / Interactive' }))
    Say '     settings: multiple-instances=IgnoreNew restart-count=999 restart-interval=1min time-limit=unlimited'
    Say ('  5. start the task: ' + $TaskName)
    Say ''
    Say '[DRY-RUN] done.'
    if ($Status) { Show-Status }
    exit 0
}

New-Item -ItemType Directory -Force -Path $DirFull | Out-Null
Write-TextNoBom $cfgPath $cfgJson
Say 'config written'
# The launcher is read by cmd.exe, which uses the ANSI/OEM code page: write it with
# -Encoding Default so non-ASCII install paths survive (the .ps1 itself stays ASCII).
Set-Content -LiteralPath $launcherPath -Value $launcherText -Encoding Default
Say 'launcher written'

Register-GpmTask -Launcher $launcherPath -Desc ('gpm probe agent (' + $NodeName + ') -> ' + $ServerUrl)
Say ('task registered: ' + $TaskName)
Enable-ScheduledTask -TaskName $TaskName | Out-Null
Start-ScheduledTask -TaskName $TaskName
Say ('task started: ' + $TaskName)

Start-Sleep -Seconds 2
$t = Get-GpmTask
if ($t) { Say ('state after start: ' + $t.State) }
Say ('verify with: powershell -NoProfile -ExecutionPolicy Bypass -File ' + (Join-Path $ScriptDir 'install-agent.ps1') + ' -Status')
if ($Status) { Show-Status }
exit 0
