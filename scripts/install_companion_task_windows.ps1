# Register the companion app as the scheduled task cw-companion on this Windows machine: the second task
# beside cw-live (the bot). It serves the page on 127.0.0.1:8765, starts itself, and restarts on a crash.
#   powershell -ExecutionPolicy Bypass -File scripts\install_companion_task_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_companion_task_windows.ps1 -Port 8780
#   powershell -ExecutionPolicy Bypass -File scripts\install_companion_task_windows.ps1 -Interactive
# Logon:
#   * elevated (admin, e.g. over WinRM like cwr install) -> S4U, runs at boot whether anyone is logged on
#     or not, no password stored. This is the full parity with cw-live.
#   * -Interactive -> a password logon (RunLevel Highest), like cw-live; only needed if you will drive the
#     YouTube Chrome sign-in from the page while signed out of the desktop.
#   * not elevated -> falls back to an "at log on, current user" task (no admin needed): persistent while
#     the box stays logged on, but it does not start before someone logs on. Run this elevated once for
#     boot-without-login.
# Stop: Stop-ScheduledTask cw-companion. Remove: Unregister-ScheduledTask cw-companion.
param([int]$Port = 8765, [switch]$Interactive)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$user = "$env:USERDOMAIN\$env:USERNAME"
$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

$arg = "-ExecutionPolicy Bypass -File `"$repo\scripts\run_companion_windows.ps1`" -Port $Port"
$act = New-ScheduledTaskAction -Execute powershell.exe -WorkingDirectory $repo -Argument $arg
$set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable

Unregister-ScheduledTask cw-companion -Confirm:$false -ErrorAction SilentlyContinue
if ($Interactive) {
    $trg = New-ScheduledTaskTrigger -AtStartup; $trg.Delay = "PT1M"
    $cred = Get-Credential -UserName $user -Message "Password of $user, for the cw-companion task"
    Register-ScheduledTask cw-companion -Action $act -Trigger $trg -Settings $set -RunLevel Highest `
        -User $user -Password $cred.GetNetworkCredential().Password | Out-Null
    $how = "password logon, at boot"
}
elseif ($elevated) {
    $trg = New-ScheduledTaskTrigger -AtStartup; $trg.Delay = "PT1M"
    $prin = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
    Register-ScheduledTask cw-companion -Action $act -Trigger $trg -Settings $set -Principal $prin | Out-Null
    $how = "S4U, at boot whether logged on or not"
}
else {
    # No admin: an at-log-on task for this user is all we can register. Persistent while logged on.
    $trg = New-ScheduledTaskTrigger -AtLogOn -User $user; $trg.Delay = "PT30S"
    Register-ScheduledTask cw-companion -Action $act -Trigger $trg -Settings $set | Out-Null
    $how = "at log on (not elevated: run this elevated once for boot-without-login)"
}
Start-ScheduledTask cw-companion
Start-Sleep 15
$t = Get-ScheduledTask cw-companion
"task cw-companion: $($t.State), $how"
"page (this machine): http://127.0.0.1:$Port/"
$ts = "C:\Program Files\Tailscale\tailscale.exe"; if (-not (Test-Path $ts)) { $ts = (Get-Command tailscale -EA SilentlyContinue).Source }
if ($ts) { try { $n = ((& $ts status --json | ConvertFrom-Json).Self.DNSName).TrimEnd('.'); if ($n) { "page (tailnet, HTTPS): https://$n/" } } catch {} }
"logs: $repo\work\logs\companion-$(Get-Date -Format yyyyMMdd).log"
