[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SpreadsheetId,
    [switch]$EnableLiveSources,
    [switch]$EnableAi
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project virtual-environment Python was not found: $python"
}

$logDirectory = Join-Path $projectRoot "runtime\logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logDirectory "scheduled-$stamp.log"
$arguments = @(
    "-m", "jobhunt.cli", "run", "--write", "--backend", "google",
    "--spreadsheet-id", $SpreadsheetId, "--summary-only"
)
if ($EnableLiveSources) { $arguments += "--live" }
if ($EnableAi) { $arguments += "--enable-ai" }

Push-Location $projectRoot
try {
    $env:JOBHUNT_TRIGGER = "windows-task-scheduler"
    & $python @arguments *>> $logPath
    exit $LASTEXITCODE
}
finally {
    Remove-Item Env:\JOBHUNT_TRIGGER -ErrorAction SilentlyContinue
    Pop-Location
}
