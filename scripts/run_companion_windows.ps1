# The companion app (clipwatcher.ui) as a supervised service, the twin of run_live_windows.ps1.
#   powershell -ExecutionPolicy Bypass -File scripts\run_companion_windows.ps1              # port 8765, Tailscale
#   powershell -ExecutionPolicy Bypass -File scripts\run_companion_windows.ps1 -Port 8780
#   powershell -ExecutionPolicy Bypass -File scripts\run_companion_windows.ps1 -NoTailscale # loopback only
# It loads .env the same way the live lane does, then keeps the page server up: a crash comes back after a
# short wait. With Tailscale present it publishes the page on the tailnet over HTTPS with `tailscale serve`
# (so the PWA installs on a phone) and tells the app to trust the tailnet name. The task cw-companion runs
# this (see install_companion_task_windows.ps1). Ctrl-C stops it.
param([int]$Port = 8765, [switch]$NoTailscale)

Set-Location (Split-Path $PSScriptRoot -Parent)
if (Test-Path .env) {
    # the app reads .env itself for keys; still load it here so CLIPWATCHER_* set in .env reach the server
    foreach ($line in Get-Content .env) {
        if ($line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $name = $Matches[1]
            $value = $Matches[2].Trim()
            if ($value -match '^"([^"]*)"' -or $value -match "^'([^']*)'") { $value = $Matches[1] }
            else { $value = ($value -replace '(^|\s+)#.*$', '').Trim() }
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}
if (-not $env:CLIPWATCHER_FFMPEG -and (Test-Path C:\ffmpeg\bin\ffmpeg.exe)) { $env:CLIPWATCHER_FFMPEG = "C:\ffmpeg\bin\ffmpeg.exe" }
$env:PYTHONUTF8 = "1"
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
$py = "$PWD\.venv\Scripts\python.exe"

New-Item -ItemType Directory -Force work\logs | Out-Null
$root = $PWD.Path
try { $host.UI.RawUI.WindowTitle = "Clip Watcher COMPANION" } catch {}

# Publish on the tailnet over HTTPS so the page (and its PWA) work from a phone. `tailscale serve` persists
# in tailscaled, so this only re-asserts it; it proxies the tailnet name to 127.0.0.1:$Port.
$tsArgs = @()
$ts = "C:\Program Files\Tailscale\tailscale.exe"; if (-not (Test-Path $ts)) { $ts = (Get-Command tailscale -EA SilentlyContinue).Source }
if (-not $NoTailscale -and $ts) {
    try {
        $state = (& $ts status --json 2>$null | ConvertFrom-Json).BackendState
        if ($state -eq "Running") {
            & $ts serve --bg --https=443 $Port 2>&1 | Out-Null
            $tsArgs = @("--tailscale")
            "tailscale serve: https://$(((& $ts status --json | ConvertFrom-Json).Self.DNSName).TrimEnd('.'))/ -> 127.0.0.1:$Port"
        } else { "tailscale not running (state $state): serving loopback only" }
    } catch { "tailscale serve setup failed: $_" }
}

# Supervisor: a crashed page server comes back; only stopping the task (which kills this script) ends it.
while ($true) {
    & $py -m clipwatcher.ui --host 127.0.0.1 --port $Port @tsArgs 2>&1 | ForEach-Object {
        $l = "$_"; $l
        [IO.File]::AppendAllText("$root\work\logs\companion-$(Get-Date -Format yyyyMMdd).log", $l + "`r`n")
    }
    $msg = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ERROR run_companion: page server exited (code $LASTEXITCODE); restarting in 10 s"
    $msg; [IO.File]::AppendAllText("$root\work\logs\companion-$(Get-Date -Format yyyyMMdd).log", $msg + "`r`n")
    Start-Sleep 10
}
