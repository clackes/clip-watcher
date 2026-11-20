# Windows twin of run_live_mac.sh: Telegram approval bot + live lane, with .env loaded.
#   powershell -ExecutionPolicy Bypass -File scripts\run_live_windows.ps1                  # config\live_streamers.txt, else all
#   powershell -ExecutionPolicy Bypass -File scripts\run_live_windows.ps1 kaicenat,clix    # a subset
#   powershell -ExecutionPolicy Bypass -File scripts\run_live_windows.ps1 -YtLogin -Account <username>  # YouTube Studio login
# Ctrl-C (or closing the window) stops both.
param([string[]]$Streamers = @(), [switch]$YtLogin, [string]$Account = "")

Set-Location (Split-Path $PSScriptRoot -Parent)
if (Test-Path .env) {
    # Read KEY=value lines the way the shell does in run_live_mac.sh: a value may be quoted, and an unquoted
    # value ends where a comment starts (" #", or "#" right after the "="). Without that, .env.example's
    # "TELEGRAM_BOT_TOKEN=     # from @BotFather" became a token made of the comment and the live lane died
    # at start. An empty value leaves the variable unset.
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

if ($YtLogin) {
    $a = @(); if ($Account) { $a = @("--account", $Account) }
    & $py scripts\yt_browser_login.py @a; exit $LASTEXITCODE
}

New-Item -ItemType Directory -Force work\logs | Out-Null
$root = $PWD.Path
$bot = $null
function Start-TelegramBot {
    if (-not $env:TELEGRAM_BOT_TOKEN) { "TELEGRAM_BOT_TOKEN not set: clips are staged in staging\ without approval cards"; return }
    if ($script:bot -and -not $script:bot.HasExited) { return }
    $running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*clipwatcher.queue.telegram_bot*" }
    if ($running) { "telegram bot already running"; return }
    $day = Get-Date -Format yyyyMMdd
    $script:bot = Start-Process $py -ArgumentList "-m", "clipwatcher.queue.telegram_bot" -NoNewWindow -PassThru `
        -RedirectStandardOutput "work\logs\telegram-$day.log" -RedirectStandardError "work\logs\telegram-$day.err.log"
    "telegram bot started (pid $($script:bot.Id), log work\logs\telegram-$day.log)"
}

# Keep the server from sleeping while the lane runs (no-op on servers that never sleep).
powercfg /change standby-timeout-ac 0 2>$null | Out-Null
try { $host.UI.RawUI.WindowTitle = "Clip Watcher LIVE" } catch {}
# No list on the command line: config\live_streamers.txt (written by cw_remote.py restart), else every streamer.
if (-not $Streamers -and (Test-Path config\live_streamers.txt)) {
    $Streamers = @((Get-Content config\live_streamers.txt -Raw) -split '[,\s]+' | Where-Object { $_ })
}
$lane = @("-m", "clipwatcher.live")
if ($Streamers) { $lane += @("--streamers", ($Streamers -join ",")) }
try {
    # Supervisor: a crashed lane or Telegram bot comes back; only stop/restart (which kill this script) end it.
    while ($true) {
        Start-TelegramBot
        # Tee-Object -Append writes UTF-16 on Windows PowerShell 5.1; append UTF-8 lines ourselves, into the
        # file of the current day (the lane runs across midnight).
        & $py @lane 2>&1 | ForEach-Object {
            $l = "$_"; $l
            [IO.File]::AppendAllText("$root\work\logs\live-$(Get-Date -Format yyyyMMdd).log", $l + "`r`n")
        }
        $msg = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ERROR run_live: live lane exited (code $LASTEXITCODE); restarting in 15 s"
        $msg; [IO.File]::AppendAllText("$root\work\logs\live-$(Get-Date -Format yyyyMMdd).log", $msg + "`r`n")
        Start-Sleep 15
    }
} finally {
    if ($bot -and -not $bot.HasExited) { taskkill /T /F /PID $bot.Id | Out-Null }
}
