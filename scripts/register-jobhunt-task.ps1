[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$SpreadsheetId,
    [switch]$EnableLiveSources,
    [switch]$EnableAi,
    [string]$TaskName = "JobHuntAutomation"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Create the project .venv before registering the task."
}

$config = Get-Content -Raw -LiteralPath (Join-Path $projectRoot "config\analysis.json") | ConvertFrom-Json
$sources = Get-Content -Raw -LiteralPath (Join-Path $projectRoot "config\sources.json") | ConvertFrom-Json
if ($EnableLiveSources -and -not ($sources | Where-Object { $_.enabled -and $_.owner_approved })) {
    throw "Live sources were requested, but no source has both enabled=true and owner_approved=true."
}
if ($EnableAi -and (-not $config.gemini.enabled -or -not $config.gemini.privacy_acknowledged_at)) {
    throw "AI was requested, but the Gemini configuration gates are incomplete."
}
$credentialArguments = @("-m", "jobhunt.cli", "credentials", "status", "--require-google")
if ($EnableAi) { $credentialArguments += "--require-gemini" }
& $python @credentialArguments
if ($LASTEXITCODE -ne 0) {
    throw "Required credentials are missing from Windows Credential Manager."
}

$runner = Join-Path $projectRoot "scripts\run-jobhunt.ps1"
$runnerArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -SpreadsheetId `"$SpreadsheetId`""
if ($EnableLiveSources) { $runnerArguments += " -EnableLiveSources" }
if ($EnableAi) { $runnerArguments += " -EnableAi" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $runnerArguments -WorkingDirectory $projectRoot
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At "07:17"
$triggers = @($dailyTrigger)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5) -RunOnlyIfNetworkAvailable
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal

if ($PSCmdlet.ShouldProcess($TaskName, "Register local daily job-hunt task")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Write-Output "Registered $TaskName for 07:17 local time."
}
