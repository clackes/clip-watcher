# Clip Watcher

Watches Twitch (and Kick) streams **while they are live**, reads chat anonymously, spots the moment chat
explodes, cuts that moment from a rolling recording, adds word-by-word captions and the branded 9:16
template, and stages the clip for approval, usually within about a minute of the moment. No login needed.

## Run it on this Mac (test)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,judge,telegram,live,calibrate,publish,publish-browser]" streamlink==8.6.3
cp .env.example .env && cp config/streamers.example.yaml config/streamers.yaml
scripts/run_live_mac.sh illojuan,teegrizzley      # stops the Mac sleeping while it runs
```

Clips land in `staging/<clip_id>/` (`short_9x16.mp4`, `clean_16x9.mp4`, `meta.json`). Streamers don't need
to be in `streamers.yaml`: each clip's layout (webcam / gameplay / full-cam) is detected from its own frames.

## The companion app

```bash
.venv/bin/python -m clipwatcher.ui --open        # http://127.0.0.1:8765, this machine only
```

One page to run the bot and see what it does: pick the live lane, a finished broadcast or the test suite,
watch each step as it happens (join, read the chat, spike, cut, judge, render, stage), then watch the clip.
Clips lists everything staged, with how each scored and settings for that one video (caption style, layout,
render again). Insights answers one question at a time: what each step costs in seconds, what the judge
costs in dollars, what the judge decided, what the detector found. Settings changes a chosen few values for
this machine (`config/local.yaml`, laid over `config/global.yaml`). Streamers appear with their Twitch or
Kick picture, name and badges, fetched once a day and kept in `work/ui/profiles/`; a finished run can be
watched again. It needs nothing installed beyond the project, starts the same commands you would type,
and never approves or uploads: that stays in Telegram.

## Approve and publish

With `TELEGRAM_BOT_TOKEN` in `.env` and `config/operators.yaml` (team chat id, operators' Telegram ids)
every clip gets a card with the video in the team chat: Claim (15 min, then anyone can claim again),
Edit the title, Approve or Reject. Approve builds `staging/approved/<clip_id>/` and uploads the Short to
YouTube (`publish.youtube_on_approve`); a failed upload shows on the card with a Retry button, and clips
approved but not uploaded are picked up when the bot starts again. `DRY_RUN=1` (the default) publishes
nothing.

- Desktop (default `publish.youtube_mode: browser`): real Chrome through patchright (the `publish-browser`
  extra; Google Chrome must be installed). Sign in once: `.venv/bin/python scripts/yt_browser_login.py`
  (Windows: `scripts\run_live_windows.ps1 -YtLogin`).
- Server without a display (Docker, VPS): the YouTube Data API (the `publish` extra). Set
  `CLIPWATCHER_YOUTUBE_MODE=api` (or `publish.youtube_mode: api`) and
  `YOUTUBE_CREDENTIALS=secrets/youtube_token.json`, a token made once with `scripts/youtube_auth.py`.

The whole chat of every broadcast is kept in `work/chat/<login>/<stream id>.jsonl` (never deleted, unlike the
video). Starting mid-stream, the bot first reads what was said before it joined from Twitch's chat replay, so
a spike is measured against the whole stream. Try the detector on any VOD:
`python scripts/replay_chat.py --vod <id> --login <channel>`.

Past broadcasts (Day-0 VOD lane): `python -m clipwatcher.vod --vod <twitch VOD url> --streamer NAME`.
Check one live stream end to end without waiting for a spike:
`python scripts/force_clip.py work/ring/<login>/<stream_id> <login>`.

## Run it on a NAS (Docker)

```bash
cp .env.example .env    # fill TELEGRAM_BOT_TOKEN for approval cards
cp config/streamers.example.yaml config/streamers.yaml   # and config/operators.yaml: both are mounted
CLIPWATCHER_DATA_DIR=/volume1/clipwatcher docker compose -f deploy/docker/compose.yaml up -d --build
```

Two containers, `live` (the lane) and `telegram` (buttons and uploads), share `/data`; clip packages and
the queue are in `/data/staging`. The image uploads through the YouTube Data API
(`CLIPWATCHER_YOUTUBE_MODE=api`): put the token from `scripts/youtube_auth.py` in `secrets/` and
`YOUTUBE_CREDENTIALS=secrets/youtube_token.json` in `.env`. A VPS with systemd:
[deploy/README.md](deploy/README.md).

Needs ~4 CPU cores per 2-3 live streamers (captions + render ~25 s per clip on an M-series Mac) and
~1.5-2.7 GB/h of disk per streamer inside the 30 min ring buffer window (old segments are deleted).

Rules, decisions and module map: [CLAUDE.md](CLAUDE.md). Roadmap: [docs/ROADMAP.md](docs/ROADMAP.md).
