# Clip Watcher

Watches Twitch (and Kick) streams **while they are live**, reads chat anonymously, spots the moment chat
explodes, cuts that moment from a rolling recording, adds word-by-word captions and the branded 9:16
template, and stages the clip for approval, usually within about a minute of the moment. No login needed.

## Run it on this Mac (test)

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev,judge,telegram,live,calibrate]" streamlink==8.6.3
cp .env.example .env && cp config/streamers.example.yaml config/streamers.yaml
scripts/run_live_mac.sh illojuan,teegrizzley      # stops the Mac sleeping while it runs
```

Clips land in `staging/<clip_id>/` (`short_9x16.mp4`, `clean_16x9.mp4`, `meta.json`). Streamers don't need
to be in `streamers.yaml`: each clip's layout (webcam / gameplay / full-cam) is detected from its own frames.

Past broadcasts (Day-0 VOD lane): `python -m clipwatcher.vod --vod <twitch VOD url> --streamer NAME`.
Check one live stream end to end without waiting for a spike:
`python scripts/force_clip.py work/ring/<login>/<stream_id> <login>`.

## Run it on a NAS (Docker)

```bash
cp .env.example .env    # fill TELEGRAM_BOT_TOKEN for approval cards
CLIPWATCHER_DATA_DIR=/volume1/clipwatcher docker compose -f deploy/docker/compose.yaml up -d --build
```

Needs ~4 CPU cores per 2-3 live streamers (captions + render ~25 s per clip on an M-series Mac) and
~1.5-2.7 GB/h of disk per streamer inside the 30 min ring buffer window (old segments are deleted).

Rules, decisions and module map: [CLAUDE.md](CLAUDE.md). Roadmap: [docs/ROADMAP.md](docs/ROADMAP.md).
