# Registers a task that runs JobPilot every 4 hours from 8 AM. Run once in PowerShell.
$bat = Join-Path $PSScriptRoot "run.bat"
$action = New-ScheduledTaskAction -Execute $bat
$trigger = New-ScheduledTaskTrigger -Once -At 8:00AM -RepetitionInterval (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName "JobPilot" -Action $action -Trigger $trigger -Description "JobPilot job search" -Force
Write-Host "Scheduled. Check it in Task Scheduler > JobPilot."
