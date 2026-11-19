# VPS deploy (netcup, Debian/Ubuntu)

1. Base: `apt install ffmpeg chromium ufw fail2ban python3.12-venv git`
2. `useradd -m -s /usr/sbin/nologin clipbot`; `git clone` to `/opt/clip-watcher`; in it
   `python3.12 -m venv .venv && .venv/bin/pip install -e ".[judge,telegram,live,calibrate,publish]" streamlink==8.6.3`;
   check `.venv/bin/streamlink --version`. Then `chown -R clipbot: /opt/clip-watcher` (the lane writes its
   work folders under the checkout unless told otherwise).
3. Config: `cp config/streamers.example.yaml config/streamers.yaml`, `config/operators.yaml` with the team
   chat id and the operators' Telegram ids.
4. Settings in `/etc/clipwatcher/env` (not secret):
   ```
   DRY_RUN=1
   CLIPWATCHER_DATA=/var/lib/clipwatcher/work
   CLIPWATCHER_STAGING=/var/lib/clipwatcher/staging
   CLIPWATCHER_YOUTUBE_MODE=api
   YOUTUBE_CREDENTIALS=/etc/clipwatcher/youtube_token.json
   ```
   Secrets in `/etc/clipwatcher/secrets.env` (root:root 0600): `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`
   (and `TWITCH_CLIENT_ID/SECRET`, `KICK_CLIENT_ID/SECRET` for the watcher unit). systemd reads both files
   itself (`EnvironmentFile=`) before it drops to `clipbot`, so the secrets file stays root-only. One
   `KEY=value` per line, no quotes, no `# comment` after a value.
   `youtube_token.json` comes from `scripts/youtube_auth.py` (run it on a desktop, copy the token over,
   `chown clipbot` + `chmod 600`). A VPS has no display, so the browser publisher (the desktop default)
   cannot run here: `CLIPWATCHER_YOUTUBE_MODE=api` selects the Data API without editing
   `config/global.yaml`. With `DRY_RUN=1` and a token, Approve uploads as private.
5. `mkdir -p /var/lib/clipwatcher && chown clipbot: /var/lib/clipwatcher`
6. `cp deploy/systemd/*.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now clipwatcher-live clipwatcher-telegram`
7. Firewall: `ufw default deny incoming; ufw allow OUTGOING; ufw allow 22/tcp; ufw enable`; `systemctl enable --now fail2ban`.
8. Keep DRY_RUN=1 until the first supervised run is verified. Ring buffer needs >= 15% free disk (pruning halts and alerts below it).

## Units

| Unit | Runs | Type |
|---|---|---|
| `clipwatcher-live` | `python -m clipwatcher.live`: record, chat, detect, clip, stage, post the Telegram card | simple |
| `clipwatcher-telegram` | `python -m clipwatcher.queue.telegram_bot`: buttons, upload on Approve, cards that went missing, expired claims | simple |
| `clipwatcher-worker` | `python -m clipwatcher.worker`: batch jobs queued with `--enqueue-media` (optional) | simple |
| `clipwatcher-watcher` | `python -m clipwatcher.ingest.watcher`: Helix/Kick-polled recorder only (optional) | notify, WatchdogSec=180 |

`live` + `telegram` are the pair to run, the same two processes as `deploy/docker/compose.yaml` and
`scripts/run_live_mac.sh`. The watcher is the only process that sends `READY=1` / `WATCHDOG=1`, so it is the
only `Type=notify` unit. Do not run it next to the live lane: both record into `ring/<login>/` and the live
lane deletes ring folders it did not create (the unit has `Conflicts=clipwatcher-live.service`).
