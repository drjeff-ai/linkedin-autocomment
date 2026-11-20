<#
.SYNOPSIS
    Register (or remove) the Windows task that tops Buffer up for one channel
    three times a day (D-022 F6, DEC-048).

.DESCRIPTION
    One task, "LinkedInCommentorTopup-<Profile>-<Platform>", with three daily
    triggers at 07:00, 13:00 and 21:00 local time. Each run executes:

        uv.exe run --no-sync python tools\run_scheduled_posts.py topup
            --profile <Profile> --platform <Platform>
            --log-file data\logs\topup.log

    from this repository, through the full path to uv.exe, as the current user,
    only when that user is logged on (no stored password). The command itself
    takes the channel's publisher lock and refuses while the drain is on, so a
    run that overlaps the dashboard sends nothing twice.

    Run it yourself, in PowerShell, from the repository root:

        powershell -ExecutionPolicy Bypass -File tools\setup_topup_task.ps1 -Profile tinderbox -Platform pinterest
        powershell -ExecutionPolicy Bypass -File tools\setup_topup_task.ps1 -Profile tinderbox -Platform pinterest -Remove

.PARAMETER Profile
    The app profile, e.g. tinderbox.
.PARAMETER Platform
    The channel, e.g. pinterest.
.PARAMETER Remove
    Unregister the task instead.
#>
param(
    [Parameter(Mandatory = $true)][string]$Profile,
    [Parameter(Mandatory = $true)][string]$Platform,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

if ($Profile -notmatch '^[A-Za-z0-9_-]+$' -or $Platform -notmatch '^[A-Za-z0-9_-]+$') {
    throw "Profile and Platform may contain only letters, digits, '_' and '-'."
}

$TaskName = "LinkedInCommentorTopup-$Profile-$Platform"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed task $TaskName"
    return
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Uv = (Get-Command uv.exe -ErrorAction Stop).Source
$Arguments = "run --no-sync python tools\run_scheduled_posts.py topup --profile $Profile --platform $Platform --log-file data\logs\topup.log"

$Action = New-ScheduledTaskAction -Execute $Uv -Argument $Arguments -WorkingDirectory $RepoRoot
$Triggers = @(
    New-ScheduledTaskTrigger -Daily -At '07:00'
    New-ScheduledTaskTrigger -Daily -At '13:00'
    New-ScheduledTaskTrigger -Daily -At '21:00'
)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers -Settings $Settings -Principal $Principal -Description "Top Buffer up for $Profile/$Platform from queued rows (linkedin.commentor topup)." -Force | Out-Null

Write-Output "Registered task $TaskName"
Write-Output "  runs: $Uv $Arguments"
Write-Output "  in:   $RepoRoot"
Write-Output "  at:   07:00, 13:00, 21:00 daily"
