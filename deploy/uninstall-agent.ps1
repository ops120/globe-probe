<#
  deploy/uninstall-agent.ps1 -- convenience wrapper around:
      install-agent.ps1 -Uninstall
  Stops and removes the gpm agent scheduled task (idempotent). The data directory
  (config file, logs, node credentials) is kept on purpose - delete it manually if
  you really want a full wipe.
  KEEP THIS FILE PURE ASCII (7-bit, no BOM) - see .docs/DEPLOY.md section 1.5 (local only).
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'gpm-agent',
    [string]$DataDir = '',
    [switch]$Status
)

$installer = Join-Path $PSScriptRoot 'install-agent.ps1'
if (-not (Test-Path -LiteralPath $installer)) {
    Write-Host ('[gpm][error] installer not found: ' + $installer)
    exit 1
}
$argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installer, '-Uninstall', '-TaskName', $TaskName)
if ($DataDir) { $argv += @('-DataDir', $DataDir) }
if ($Status) { $argv += '-Status' }
& powershell.exe @argv
exit $LASTEXITCODE
