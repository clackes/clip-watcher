# Architecture

Small Python services on one Linux host sharing a SQLite job table (stage, state, attempts, lease_until,
payload, error). Each stage writes to a temp file and renames to a fixed path, so reruns never duplicate.

```
watcher (Helix/Kick poll) -> recorder (streamlink, 10 s .ts ring) + chatlog (IRC TLS / Kick Pusher -> JSONL)
  -> detector (D3 z-score + filters) -> confirm (viewer clips, loudness)
  -> transcribe + 480p keyframes -> Claude screen/judge (D6)
  -> render short_9x16 + clean_16x9 (D4/D5, one ffmpeg pass, D9 seek fix)
  -> staging/<clip_id>/ -> Telegram claim/approve (D7) -> publisher (YouTube/IG API, D8)
  -> staging/approved/ (synced to MacBook) -> TikTok + campaign forms via Claude in Chrome (human clicks Post)
```

The Day-0 VOD lane (`python -m clipwatcher.vod`) reuses detector, judge, renderer and queue with
TwitchDownloaderCLI instead of the live recorder. Decision log D1-D9: see CLAUDE.md.
