# One-time setup of a fresh Windows Server for the bot (run over WinRM: cw_remote.py ps this-file).
# Idempotent: every step checks first. Same versions as the first box (18 Nov): Python 3.12.10,
# Git for Windows, ffmpeg 8.1 gpl (BtbN, has libass), Chrome, VC++ runtime (Whisper).
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$dl = "C:\cw-up\dl"
New-Item -ItemType Directory -Force $dl | Out-Null

function Get-GhAsset($repo, $pattern, $tag = "latest") {
    $u = if ($tag -eq "latest") { "https://api.github.com/repos/$repo/releases/latest" } else { "https://api.github.com/repos/$repo/releases/tags/$tag" }
    $r = Invoke-RestMethod -Uri $u -Headers @{ "User-Agent" = "cw-bootstrap" }
    ($r.assets | Where-Object { $_.name -match $pattern } | Select-Object -First 1).browser_download_url
}

if (-not (Test-Path "C:\Program Files\Python312\python.exe")) {
    "python: installing 3.12.10"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile "$dl\py.exe"
    Start-Process "$dl\py.exe" -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0" -Wait
}
"python: " + (& "C:\Program Files\Python312\python.exe" --version)

if (-not (Test-Path "C:\Program Files\Git\cmd\git.exe")) {
    "git: installing"
    $u = Get-GhAsset "git-for-windows/git" '^Git-[\d.]+-64-bit\.exe$'
    Invoke-WebRequest $u -OutFile "$dl\git.exe"
    Start-Process "$dl\git.exe" -ArgumentList "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES" -Wait
}
"git: " + (& "C:\Program Files\Git\cmd\git.exe" --version)

if (-not (Test-Path "C:\ffmpeg\bin\ffmpeg.exe")) {
    "ffmpeg: installing 8.1 gpl"
    $u = Get-GhAsset "BtbN/FFmpeg-Builds" 'ffmpeg-n8\.1.*-win64-gpl-8\.1\.zip$'
    Invoke-WebRequest $u -OutFile "$dl\ff.zip"
    Expand-Archive "$dl\ff.zip" -DestinationPath "$dl\ff" -Force
    $bin = (Get-ChildItem "$dl\ff" -Recurse -Filter ffmpeg.exe | Select-Object -First 1).Directory
    New-Item -ItemType Directory -Force "C:\ffmpeg" | Out-Null
    Copy-Item $bin.FullName "C:\ffmpeg\bin" -Recurse -Force
}
"ffmpeg: " + ((& C:\ffmpeg\bin\ffmpeg.exe -version | Select-Object -First 1))

if (-not (Test-Path "C:\Program Files\Google\Chrome\Application\chrome.exe")) {
    "chrome: installing"
    Invoke-WebRequest "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi" -OutFile "$dl\chrome.msi"
    Start-Process msiexec.exe -ArgumentList "/i", "$dl\chrome.msi", "/qn", "/norestart" -Wait
}
"chrome: " + (Get-Item "C:\Program Files\Google\Chrome\Application\chrome.exe").VersionInfo.ProductVersion

if (-not (Test-Path "C:\Windows\System32\vcruntime140_1.dll")) {
    "vcredist: installing"
    Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile "$dl\vc.exe"
    Start-Process "$dl\vc.exe" -ArgumentList "/install", "/quiet", "/norestart" -Wait
}
"vcredist: ok"

# App: clone from the bundle cw_remote pushed to C:\cw-up\full.bundle, venv pinned to the first box's freeze.
$git = "C:\Program Files\Git\cmd\git.exe"
if (-not (Test-Path "C:\clip-watcher\.git") -and (Test-Path "C:\cw-up\full.bundle")) {
    "repo: cloning"
    & $git clone -q -b main "C:\cw-up\full.bundle" "C:\clip-watcher" 2>&1 | Out-Null
    & $git -C "C:\clip-watcher" config core.autocrlf false
}
if (Test-Path "C:\clip-watcher\.git") {
    cd C:\clip-watcher
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        "venv: creating"
        & "C:\Program Files\Python312\python.exe" -m venv .venv
    }
    $c = if (Test-Path "C:\cw-up\freeze.txt") { @("-c", "C:\cw-up\freeze.txt") } else { @() }
    & .venv\Scripts\python.exe -m pip install -q --upgrade pip 2>&1 | Out-Null
    & .venv\Scripts\python.exe -m pip install -q @c -e ".[judge,telegram,publish,publish-browser,live,dev]" "streamlink==8.6.3" 2>&1 | Select-Object -Last 3
    New-Item -ItemType Directory -Force work\logs, staging | Out-Null
    "app: " + (& $git log --oneline -1)
    "pip: " + ((& .venv\Scripts\python.exe -m pip freeze) | Measure-Object).Count + " packages"
}
