# Clip Watcher: install, move and run

Clip Watcher watches Twitch and Kick streams around the clock. When chat explodes it cuts the moment from
its rolling recording and has Claude judge it. It then renders a captioned 9:16 Short and posts it to the
operators' Telegram group. An operator approves it there, and the bot uploads it to YouTube.

Contents:
1. [How it works](#1-how-it-works)
2. [What you need](#2-what-you-need)
3. [Moving the bot to a new machine](#3-moving-the-bot-to-a-new-machine)
4. [Fresh install on Windows](#4-fresh-install-on-windows)
5. [Install on macOS or Linux](#5-install-on-macos-or-linux)
6. [Day to day](#6-day-to-day)
7. [Configuration](#7-configuration)
8. [The companion app](#8-the-companion-app)
9. [Commands](#9-commands)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. How it works

```
Twitch/Kick live ──> continuous recording (last 30 min kept)       ┐
live chat        ──> chat spikes measured against the whole stream ┴─> moment
moment ──> 59 s cut ──> transcript ──> Claude judges it (score 0-10, title, description)
       ──> webcam and gameplay found in the picture ──> 9:16 Short + clean 16:9 clip
       ──> card in the Telegram group ──> Claim ──> Approve ──> YouTube upload (real Chrome)
```

- A streamer is recorded only while live **in the channel's category** (GTA VI, `channel.games`).
- The judge drops weak moments, so only clips with a good score reach the group.
- **Nothing goes to YouTube without an Approve** in Telegram (scheduled auto-publishing is off).

Every rule and technical decision is in [CLAUDE.md](CLAUDE.md).

## 2. What you need

**Accounts and keys.** The move package in section 3 already carries all of these.

| What | For | Where it goes |
|---|---|---|
| Anthropic API key (Claude) | the judge: score, title, description | `.env` → `ANTHROPIC_API_KEY` |
| Telegram bot (from @BotFather) | approval cards | `.env` → `TELEGRAM_BOT_TOKEN` |
| Group id and operators' ids | who may press the buttons | `config/operators.yaml` |
| YouTube account (capybarasking) | uploads | signed in through Chrome, section 4.5 |
| Twitch client id/secret (optional) | confirms moments with viewers' clips | `.env` → `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` |

**Machine.**
- **OS:** Windows 10/11 or Windows Server is the tested setup; macOS or Linux also work.
- **CPU and memory:** about 4 cores per 2-3 streamers followed at once, 8 GB of RAM or more.
- **Disk:** about 1.5-2.7 GB per streamer for the 30 min window, plus the clips. Keep 30 GB free: below 15%
  or 5 GB free the bot stops recording and warns.
- **Network:** a steady connection, since every live stream is downloaded continuously.

**Software.** On Windows the bootstrap script installs it (section 4):
- Python 3.12, Git, ffmpeg 8.1 with libass and Google Chrome;
- the Visual C++ runtime, for the transcriber;
- optionally Node.js 20+, for the animated captions.

## 3. Moving the bot to a new machine

The code is in git. `scripts/move_bot.py` carries everything else: keys, configuration, the clip queue, saved
chats and the webcam model.

### 3.1 On the old machine: export

```powershell
cd C:\clip-watcher
.venv\Scripts\python.exe scripts\move_bot.py export
```

This writes `work\out\clipwatcher-move-<date>.zip`, a few hundred MB:

| File | Contents |
|---|---|
| `full.bundle` | the code with its whole git history |
| `bootstrap_windows.ps1` | the Windows installer (section 4.1) |
| `freeze.txt` | the exact Python package versions that work |
| `private/` | `.env`, `config/streamers.yaml`, `operators.yaml`, `local.yaml`, `live_streamers.txt`, `models/` |
| `data/` | the clip queue (`staging/` with `clips.db`), the whole chat of every broadcast (`work/chat/`), the judge's cost ledger (`work/usage/`) |
| `MANIFEST.json` | every file with its SHA-256; import checks nothing was damaged |

- `--no-data` exports keys and configuration only, a few MB. The new machine then starts with an empty queue.
- **The package contains the keys: treat it like a password.** Don't paste it into chats or upload it
  anywhere public, and delete it when you are done.
- The YouTube sign-in does **not** move. Chrome encrypts its cookies with the Windows user of that machine,
  so you sign in again on the new one (section 4.5).
- Recordings, renders, logs and caches are not exported; the bot makes them again.

To fetch the package to the Mac: `cwr get 'C:\clip-watcher\work\out\clipwatcher-move-<date>.zip' ~/Downloads/move.zip`.

### 3.2 On the new machine

1. Unzip the package into `C:\cw-up\`, so that you have `C:\cw-up\full.bundle`, `C:\cw-up\freeze.txt` and
   `C:\cw-up\bootstrap_windows.ps1`.
2. Run the installer (section 4.1). It installs the programs and clones the code from the bundle into
   `C:\clip-watcher`.
3. Put keys, config and data back in place:

   ```powershell
   cd C:\clip-watcher
   .venv\Scripts\python.exe scripts\move_bot.py import C:\path\to\clipwatcher-move-<date>.zip
   ```

   If any file already exists it stops without touching anything; `--force` overwrites.
4. Go on with section 4 from 4.4: animated captions, YouTube sign-in, start.

### 3.3 Switching over without two bots

**Only one Telegram bot per token may run.** Two copies steal each other's updates and the buttons fail at
random. Do it in this order:

1. On the old machine, run `Stop-ScheduledTask cw-live` and stop any leftover processes (section 9.1).
2. Export (3.1), so the package holds the latest queue.
3. Install and import on the new machine (3.2), then start it.
4. Once cards arrive from the new machine, run `Unregister-ScheduledTask cw-live` on the old one.

If the machine's address changes, set `CW_WINRM_HOST` for `cwr` on the Mac (section 9.3).

## 4. Fresh install on Windows

Run everything in **PowerShell as Administrator**.

### 4.1 Programs and code

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& C:\cw-up\bootstrap_windows.ps1
```

The script installs only what is missing, and it is safe to run again:
- Python 3.12.10 and Git;
- ffmpeg 8.1 into `C:\ffmpeg\bin`;
- Google Chrome and the Visual C++ runtime.

It then clones the code from `C:\cw-up\full.bundle` into `C:\clip-watcher`, creates `.venv` and installs
the Python packages pinned to `C:\cw-up\freeze.txt`.

Without a move package, copy `scripts\remote\bootstrap_windows.ps1` from the code and put a bundle at
`C:\cw-up\full.bundle`. You can make one on any machine that has the code: `git bundle create full.bundle main`.

### 4.2 Keys and configuration

With a move package, run `move_bot.py import` (3.2, step 3) and skip to 4.4. From scratch:

```powershell
cd C:\clip-watcher
Copy-Item .env.example .env
Copy-Item config\streamers.example.yaml config\streamers.yaml
Copy-Item config\operators.example.yaml config\operators.yaml
notepad .env
```

In `.env`, put one `KEY=value` per line:

```
DRY_RUN=0
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABC...
CLIPWATCHER_FFMPEG=C:\ffmpeg\bin\ffmpeg.exe
YT_BROWSER_ACCOUNT=capybarasking
CLIPWATCHER_NODE=C:\Program Files\nodejs\node.exe
```

- `DRY_RUN=1` (the default when missing) uploads nothing. Use it for trials.
- The companion app's Connect page can also set the keys and check them for free.

`config\operators.yaml` holds the Telegram group id and each operator's id. To find them, add the bot to the
group, start it and send `/id` in the group: it replies with the chat id and yours.

`config\streamers.yaml` lists the streamers to follow (section 7.1).

### 4.3 Webcam model

The bot uses it to find the webcam in the picture. A move package already has it in `models\`. From scratch:

```powershell
New-Item -ItemType Directory -Force models | Out-Null
Invoke-WebRequest https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite -OutFile models\blaze_face_short_range.tflite
```

### 4.4 Animated captions (optional)

These are only needed for the 25 animated templates (`rm:<name>`); the built-in caption styles work without them.

```powershell
winget install OpenJS.NodeJS.LTS
cd C:\clip-watcher\remotion
npm ci
npm run setup
```

Then add `CLIPWATCHER_NODE=C:\Program Files\nodejs\node.exe` to `.env`.

On a machine that is also recording, a template takes about 4 minutes per clip. Keep the live lane on the
`word` style and use templates for VODs, or to render a clip again from the companion app. Remotion is
free for teams of up to three people (see `remotion/README.md`).

### 4.5 YouTube sign-in

Do this **at the machine or over Remote Desktop**, never over WinRM or `cwr`: from there Chrome loses its
cookies.

```powershell
cd C:\clip-watcher
powershell -ExecutionPolicy Bypass -File scripts\run_live_windows.ps1 -YtLogin -Account capybarasking
```

1. Chrome opens. Sign in to the channel's Google account and open YouTube Studio.
2. Once Studio is open, close Chrome.

The profile is kept in `%USERPROFILE%\.clipwatcher\yt-profiles\capybarasking`; its name must match
`YT_BROWSER_ACCOUNT` in `.env`. The companion app's Connect → YouTube does the same.

### 4.6 Run the bot as a service

```powershell
cd C:\clip-watcher
powershell -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1
```

The script asks for the Windows user's password and registers the scheduled task `cw-live`. The password
lets Chrome read the YouTube sign-in. The task:
- starts with Windows and runs whether anyone is logged on or not;
- restarts itself if it dies, and opens no window.

Run the script again whenever the Windows password changes.

The task starts two processes: the Telegram bot (buttons and uploads) and the live lane (recording, chat,
clips). It follows the streamers in `config\live_streamers.txt` (comma-separated), or else every streamer
in `streamers.yaml`.

### 4.7 Check that it works

```powershell
Get-ScheduledTask cw-live                                  # State: Running
Get-Content work\logs\live-$(Get-Date -Format yyyyMMdd).log -Tail 20 -Wait
```

With a streamer live in GTA VI, the log shows, in this order:
1. `live in Grand Theft Auto VI, recording`
2. `baseline from N messages`
3. for each spike, `step=cut`, then `step=judge`, then either `staged` or `judge rejected` with the reason.

The Telegram bot's errors are in `work\logs\telegram-<date>.err.log`.

End to end: approve a clip in Telegram. The card should then show a `youtube.com/shorts/...` link. If the
upload fails, a screenshot of YouTube Studio is in `work\yt_shots\`.

Tests take about 6 minutes. They never touch the network and never upload:

```powershell
$env:DRY_RUN = "1"; .venv\Scripts\python.exe -m pytest -q -m "not ffmpeg and not remotion"
```

## 5. Install on macOS or Linux

```bash
git clone -b main full.bundle clip-watcher && cd clip-watcher
python3.12 -m venv .venv
.venv/bin/pip install -c ../freeze.txt -e ".[judge,telegram,live,calibrate,publish,publish-browser,dev]" streamlink==8.6.3
.venv/bin/python scripts/move_bot.py import ~/Downloads/clipwatcher-move-<date>.zip   # or .env and config by hand (4.2)
.venv/bin/python scripts/yt_browser_login.py         # YouTube sign-in (Google Chrome must be installed)
scripts/run_live_mac.sh                              # Telegram bot + live lane; keeps the Mac awake
```

- ffmpeg needs libass: on a Mac, `brew install ffmpeg-full` (the bot prefers it by itself).
- `freeze.txt` was made on Windows. If a package fails to install, drop `-c ../freeze.txt`.
- Without a display (VPS, NAS), use Docker and the YouTube Data API: see [README.md](README.md#run-it-on-a-nas-docker)
  and [deploy/README.md](deploy/README.md).

## 6. Day to day

### 6.1 Telegram

Every clip arrives in the group as a card: the video, the judge's score, title and description, and the
chat numbers.

| Button | What it does |
|---|---|
| **Claim** (one per operator) | you hold the clip for 15 minutes, then it is free again |
| **Approve** | uploads it to YouTube right away; the card then shows the link |
| **Edit** | change the title (reply with the new one) |
| **Reject** | drop it |
| **Release** | hand it back to the others |
| **🔁 Redo** | cut it again, with an optional note on what to change |
| **Retry YouTube upload** | appears only after a failed upload |

When an upload fails, the card says why. If YouTube had already received the video, the card says so: check
Studio before pressing Retry, or you upload it twice. On restart the bot uploads, by itself, any clip that was
approved but never started uploading.

### 6.2 Worth checking now and then

- **Activity:** with streamers live, the log shows `staged` or `judge rejected`.
- **Disk space:** below 15% or 5 GB free the bot warns and stops recording.
- **Judge costs:** companion app → Insights, or `work\usage\judge.jsonl`.

## 7. Configuration

General settings are in `config\global.yaml`, which is in git and the same on every machine. Changes for one
machine only go in `config\local.yaml`. That file is laid over `global.yaml` and is not in git; the companion
app's Settings page writes there.

### 7.1 Streamers

`config\streamers.yaml` has one entry per streamer, keyed by login in lowercase:

```yaml
streamers:
  kaicenat:
    platform: twitch          # twitch | kick
    url: https://www.twitch.tv/kaicenat
    language: en              # must be the channel language (en), or the streamer is skipped
    campaign_link: null       # campaign link, if any
```

The webcam position is found in the picture, so it does not need to be set. To choose who is followed right now:

```powershell
Set-Content config\live_streamers.txt "kaicenat,clix,jynxzi"
```

Then restart the bot (section 9.1).

### 7.2 Settings that matter

| Key in `global.yaml` | Value | Meaning |
|---|---|---|
| `channel.games` | GTA VI | record only in these categories (`[]` = any game) |
| `buffer.window_min` | 30 | minutes of recording kept |
| `buffer.quality` | 720p | recording quality (higher = sharper webcam, more disk) |
| `detector.max_per_hour` | 8 | at most this many moments an hour per streamer |
| `render.captions` | word | caption style (`word`, `pop`, `classic`, `bounce`, `ab`, `rm:<template>`) |
| `judge.reject_flags` | [sp] | flags that drop a clip (sp = spoiler) |
| `publish.schedule.enabled` | false | publishing without an Approve (off) |
| `publish.youtube_mode` | browser | upload through Chrome; `api` for a server without a display |

Restart the bot after a change (section 9.1).

## 8. The companion app

The companion app is a local web page for watching and trying the bot without the command line:

```powershell
.venv\Scripts\python.exe -m clipwatcher.ui --open      # http://127.0.0.1:8765, this machine only
```

- **Run**: start a trial (live, a VOD, the tests) and follow it step by step.
- **Clips**: every clip with its score and video; render the Short again with another caption style.
- **Library**: the 29 caption styles, their settings, your own templates.
- **Insights**: timings, judge cost, judge decisions, what the detector found.
- **Connect**: Claude key, Telegram token, YouTube accounts.
- **Settings**: this machine's settings.

The app never approves or uploads: that happens only in Telegram. **Do not use it to start a second live
lane or a second Telegram bot while `cw-live` is running.**

## 9. Commands

Run these from `C:\clip-watcher` in PowerShell.

### 9.1 Start, stop, restart

```powershell
Get-ScheduledTask cw-live | Select-Object State      # status
Start-ScheduledTask cw-live                           # start
```

Stop everything (task, bot processes, recordings):

```powershell
Stop-ScheduledTask cw-live
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'clipwatcher\.(live|queue)|run_live_windows' } | ForEach-Object { taskkill /T /F /PID $_.ProcessId }
Get-Process streamlink,ffmpeg -ErrorAction SilentlyContinue | Stop-Process -Force
```

To restart, stop everything, then run `Start-ScheduledTask cw-live`. First check the log for a clip in
progress (a `step=` line not yet followed by `staged`): a restart loses it.

### 9.2 Other

```powershell
.venv\Scripts\python.exe -m clipwatcher.vod --vod <Twitch VOD url> --streamer <name>     # clips from a finished broadcast
.venv\Scripts\python.exe scripts\force_clip.py work\ring\<login>\<folder> <login>         # force a clip of the last 45 s
.venv\Scripts\python.exe scripts\replay_chat.py --vod <id> --login <channel>              # try the detector on a VOD
.venv\Scripts\python.exe scripts\move_bot.py export                                      # backup or move
```

### 9.3 From the Mac (`cwr`)

On the Mac, `cwr` drives the bot remotely. It is a shell function: define it in each terminal (or in
`~/.zshrc`) and run it from the code folder.

```bash
cwr() { uv run -q --with pywinrm==0.5.0 scripts/remote/cw_remote.py "$@"; }
export CW_WINRM_HOST=<machine address>     # if it is not 140.82.40.80
cwr status ; cwr logs -n 80 ; cwr restart kaicenat,clix ; cwr stop ; cwr push ; cwr test
```

- **Password:** the Windows password lives in the Mac's Keychain, item `clipwatcher-winrm-<address>`,
  account Administrator.
- **Server:** the machine must have WinRM on, port 5985.

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| No cards, buttons do nothing | two Telegram bots on one token: keep only one running (3.3) |
| `not recording, offline` | the streamer is not live, or not in GTA VI |
| Only `judge rejected` | weak moments; the log line gives the judge's reason |
| Upload failed, `not signed in` | sign in to YouTube again, at the machine (4.5) |
| Upload failed, anything else | look at the screenshot in `work\yt_shots\`, then press Retry on the card |
| `ffmpeg not found` | `CLIPWATCHER_FFMPEG` in `.env` must point to `C:\ffmpeg\bin\ffmpeg.exe` |
| Animated captions not applied | Node or `npm run setup` is missing; the clip gets the built-in style and `meta.captions_error` says why |
| `recording paused`, low disk | free some space; the ring buffer needs 1.5-2.7 GB per streamer |
| Garbled titles, crashes on emoji | start the bot through `run_live_windows.ps1`, which sets UTF-8 |
