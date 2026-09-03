[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param([string]$TaskName = "JobHuntAutomation")

$ErrorActionPreference = "Stop"
if ($PSCmdlet.ShouldProcess($TaskName, "Unregister local job-hunt task")) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Unregistered $TaskName."
}
