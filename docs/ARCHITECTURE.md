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

## Approval queue (queue/, publish/)

One SQLite file, `<staging>/clips.db`, shared by the lanes (they add clips and post their cards) and the
approval bot (`python -m clipwatcher.queue.telegram_bot`: buttons, uploads, housekeeping). `<staging>` is
`CLIPWATCHER_STAGING`, else `<repo>/staging`; every default goes through `queue.staging.staging_root()`.

`READY -> CLAIMED -> APPROVED -> PUBLISHED`, or `REJECTED`. Each step is one guarded UPDATE.

- Claim: one operator for `publish.claim_ttl_min` (15). When it runs out anyone can claim; the bot's sweep
  (once a minute) puts the clip back to READY and redraws its card with the Claim buttons.
- Staging a clip id again (a VOD re-run) replaces the package only while the clip is still READY.
- One card per clip. The row keeps where the card is (`card_chat_id`, `card_message_id`) and a short lease
  while someone is sending it, so the lane and the bot never both post it. The sweep posts cards that are
  missing: 3 tries, the last without the video, clips up to `publish.card_max_age_h` (24; 0 = off) old.
- Approve builds `approved/<clip_id>/` (with the edited title) and uploads when
  `publish.youtube_on_approve` is on, one upload at a time. A failure is audited, kept on the row
  (`publish_error`) and shown on the card with a Retry button. At start the bot uploads what was approved
  in the last `publish.resume_max_h` (24) hours and never got to YouTube; a clip whose earlier attempt
  already has a video id is left to an operator, because it may be there.
- The `audit` table has every action, every upload failure and every video id.
