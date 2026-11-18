# Windows twin of run_live_mac.sh: Telegram approval bot + live lane, with .env loaded.
#   powershell -ExecutionPolicy Bypass -File scripts\run_live_windows.ps1                  # every English streamer
#   powershell -ExecutionPolicy Bypass -File scripts\run_live_windows.ps1 kaicenat,clix    # a subset
#   powershell -ExecutionPolicy Bypass -File scripts\run_live_windows.ps1 -YtLogin         # one-time YouTube Studio login
# Ctrl-C (or closing the window) stops both.
param([string]$Streamers = "", [switch]$YtLogin)

Set-Location (Split-Path $PSScriptRoot -Parent)
if (Test-Path .env) {
    Get-Content .env | Where-Object { $_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$' } | ForEach-Object {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim().Trim('"'), "Process")
    }
}
if (-not $env:CLIPWATCHER_FFMPEG -and (Test-Path C:\ffmpeg\bin\ffmpeg.exe)) { $env:CLIPWATCHER_FFMPEG = "C:\ffmpeg\bin\ffmpeg.exe" }
$env:PYTHONUTF8 = "1"
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
$py = "$PWD\.venv\Scripts\python.exe"

if ($YtLogin) { & $py scripts\yt_browser_login.py; exit $LASTEXITCODE }

New-Item -ItemType Directory -Force work\logs | Out-Null
$day = Get-Date -Format yyyyMMdd
$bot = $null
if ($env:TELEGRAM_BOT_TOKEN) {
    $running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*clipwatcher.queue.telegram_bot*" }
    if ($running) { "telegram bot already running" }
    else {
        $bot = Start-Process $py -ArgumentList "-m", "clipwatcher.queue.telegram_bot" -NoNewWindow -PassThru `
            -RedirectStandardOutput "work\logs\telegram-$day.log" -RedirectStandardError "work\logs\telegram-$day.err.log"
        "telegram bot started (pid $($bot.Id), log work\logs\telegram-$day.log)"
    }
} else { "TELEGRAM_BOT_TOKEN not set: clips are staged in staging\ without approval cards" }

# Keep the server from sleeping while the lane runs (no-op on servers that never sleep).
powercfg /change standby-timeout-ac 0 2>$null | Out-Null
try {
    $lane = @("-m", "clipwatcher.live")
    if ($Streamers) { $lane += @("--streamers", $Streamers) }
    & $py @lane 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath "work\logs\live-$day.log" -Append
} finally {
    if ($bot -and -not $bot.HasExited) { taskkill /T /F /PID $bot.Id | Out-Null }
}
