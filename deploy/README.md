# VPS deploy (netcup, Debian/Ubuntu)

1. Base: `apt install ffmpeg chromium ufw fail2ban python3.12-venv git`
2. `pipx install streamlink==8.6.3` (or in the venv); check `streamlink --version`.
3. `useradd -m -s /usr/sbin/nologin clipbot`; `git clone` to `/opt/clip-watcher`; `python3.12 -m venv .venv && .venv/bin/pip install -e ".[live]"`.
4. Secrets: `/etc/clipwatcher/secrets.env` (root:root 0600; TWITCH_CLIENT_ID/SECRET, KICK_CLIENT_ID/SECRET,
   Telegram token, API keys). Non-secret settings (`DRY_RUN`, `CLIPWATCHER_DATA=/var/lib/clipwatcher`) in `/etc/clipwatcher/env`.
   Services read secrets from `$CREDENTIALS_DIRECTORY/secrets.env` via LoadCredential.
5. `mkdir -p /var/lib/clipwatcher && chown clipbot: /var/lib/clipwatcher`
6. `cp deploy/systemd/*.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now clipwatcher-{watcher,worker,telegram}`
7. Firewall: `ufw default deny incoming; ufw allow OUTGOING; ufw allow 22/tcp; ufw enable`; `systemctl enable --now fail2ban`.
8. Keep DRY_RUN=1 until the first supervised run is verified. Ring buffer needs >= 15% free disk (pruning halts and alerts below it).

Note: all three units use Type=notify + WatchdogSec=60, so each process must send READY=1 and WATCHDOG=1
(`clipwatcher.ingest.watcher.sd_notify`; only the watcher does so today). The worker/telegram entry points are assumed
names. Secrets come via LoadCredential, so the app must read `$CREDENTIALS_DIRECTORY/secrets.env`.
