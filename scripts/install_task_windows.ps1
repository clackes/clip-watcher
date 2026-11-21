# Register the bot as the scheduled task cw-live on this Windows machine (what `cwr install` does from the
# Mac): it runs whether anyone is logged on or not, starts at boot, restarts itself, has no window.
#   powershell -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1
# Asks for this Windows user's password: the task needs a password logon, because Chrome's YouTube cookies
# are encrypted with the user's real credentials (S4U or a network logon would sign YouTube out).
# Run it again after changing the password. Stop: Stop-ScheduledTask cw-live. Remove: Unregister-ScheduledTask cw-live.
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$user = "$env:USERDOMAIN\$env:USERNAME"
$cred = Get-Credential -UserName $user -Message "Password of $user, for the cw-live task"
$pw = $cred.GetNetworkCredential().Password

$arg = "-ExecutionPolicy Bypass -File `"$repo\scripts\run_live_windows.ps1`""
$act = New-ScheduledTaskAction -Execute powershell.exe -WorkingDirectory $repo -Argument $arg
$trg = New-ScheduledTaskTrigger -AtStartup
$trg.Delay = "PT1M"
$set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable
Unregister-ScheduledTask cw-live -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask cw-live -Action $act -Trigger $trg -Settings $set -RunLevel Highest `
    -User $user -Password $pw | Out-Null
Start-ScheduledTask cw-live
Start-Sleep 20
$t = Get-ScheduledTask cw-live
"task cw-live: $($t.State), logon $($t.Principal.LogonType)"
"logs: $repo\work\logs\live-$(Get-Date -Format yyyyMMdd).log"
