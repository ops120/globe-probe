# Stop the local gpm server.
# ASCII-only on purpose: Windows PowerShell 5.1 decodes BOM-less UTF-8 files as GB2312,
# and non-ASCII bytes there can swallow the following LF (merged lines => broken script).
# Match only the python interpreter running "-m gpm server", so this script can never kill
# the PowerShell process that runs it.
$procs = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -match '-m\s+gpm\s+server' }
if (-not $procs) { Write-Host "no running gpm server process"; exit 0 }
foreach ($p in $procs) {
  Write-Host "kill PID $($p.ProcessId): $($p.CommandLine)"
  Stop-Process -Id $p.ProcessId -Force
}
