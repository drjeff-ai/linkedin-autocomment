<#
.SYNOPSIS
    Register (or remove) the two Windows tasks that run one profile's YouTube
    comments on a schedule (D-033 Y2-S, DEC-076). Approval stays a person's.

.DESCRIPTION
    Two tasks, run from this repository through the full path to uv.exe, as
    the current user, only when that user is logged on (no stored password):

    "LinkedInCommentorYouTubeMorning-<Profile>", daily at 07:30, 20-minute limit:

        uv.exe run --no-sync python tools\run_scheduled_posts.py youtube-morning
            --profile <Profile> --scheduled --log-file data\logs\youtube-engage.log

    It finds new watch-list videos and viewers' comments, then drafts up to
    today's cap minus the drafts already waiting. It approves nothing.

    "LinkedInCommentorYouTubeEngage-<Profile>", daily from 08:00, every 15
    minutes for 13 hours, 10-minute limit:

        uv.exe run --no-sync python tools\run_scheduled_posts.py youtube-engage
            --profile <Profile> --scheduled --log-file data\logs\youtube-engage.log

    It posts at most one comment a run, and only one a person approved, under
    the window, gap, ramp, creator and item pins. A run with nothing to post
    makes no Google call.

    Both commands refuse unless the profile's engagement config says
    "auto_run": true.

    Run it yourself, in PowerShell, from the repository root:

        powershell -ExecutionPolicy Bypass -File tools\setup_youtube_engage_tasks.ps1 -Profile topfive
        powershell -ExecutionPolicy Bypass -File tools\setup_youtube_engage_tasks.ps1 -Profile topfive -Remove

.PARAMETER Profile
    The app profile, e.g. topfive.
.PARAMETER Remove
    Unregister both tasks instead.
#>
param(
    [Parameter(Mandatory = $true)][string]$Profile,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

if ($Profile -notmatch '^[A-Za-z0-9_-]+$') {
    throw "Profile may contain only letters, digits, '_' and '-'."
}

$MorningTask = "LinkedInCommentorYouTubeMorning-$Profile"
$EngageTask = "LinkedInCommentorYouTubeEngage-$Profile"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $MorningTask -Confirm:$false
    Unregister-ScheduledTask -TaskName $EngageTask -Confirm:$false
    Write-Output "Removed tasks $MorningTask and $EngageTask"
    return
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Uv = (Get-Command uv.exe -ErrorAction Stop).Source
$MorningArguments = "run --no-sync python tools\run_scheduled_posts.py youtube-morning --profile $Profile --scheduled --log-file data\logs\youtube-engage.log"
$EngageArguments = "run --no-sync python tools\run_scheduled_posts.py youtube-engage --profile $Profile --scheduled --log-file data\logs\youtube-engage.log"

$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

$MorningAction = New-ScheduledTaskAction -Execute $Uv -Argument $MorningArguments -WorkingDirectory $RepoRoot
$MorningTrigger = New-ScheduledTaskTrigger -Daily -At '07:30'
$MorningSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

$EngageAction = New-ScheduledTaskAction -Execute $Uv -Argument $EngageArguments -WorkingDirectory $RepoRoot
# A daily trigger cannot take a repetition directly in PowerShell 5.1, so it
# borrows one from a one-off trigger: every 15 minutes for 13 hours (to 21:00).
$EngageTrigger = New-ScheduledTaskTrigger -Daily -At '08:00'
$EngageTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At '08:00' -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 13)).Repetition
$EngageSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $MorningTask -Action $MorningAction -Trigger $MorningTrigger -Settings $MorningSettings -Principal $Principal -Description "Find and draft YouTube comments for $Profile; approves nothing (linkedin.commentor youtube-morning)." -Force | Out-Null
Register-ScheduledTask -TaskName $EngageTask -Action $EngageAction -Trigger $EngageTrigger -Settings $EngageSettings -Principal $Principal -Description "Post at most one approved YouTube comment for $Profile when pacing allows (linkedin.commentor youtube-engage)." -Force | Out-Null

Write-Output "Registered task $MorningTask"
Write-Output "  runs: $Uv $MorningArguments"
Write-Output "  at:   07:30 daily"
Write-Output "Registered task $EngageTask"
Write-Output "  runs: $Uv $EngageArguments"
Write-Output "  at:   every 15 minutes, 08:00 to 21:00 daily"
Write-Output "  in:   $RepoRoot"
