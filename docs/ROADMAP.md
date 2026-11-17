# Roadmap

| Phase | Scope | Exit check |
|---|---|---|
| 0 | Channel (Brand Account), GCP project in Production, Twitch app, Telegram bot, VPS, streamer list, replay library | one test upload comes back public |
| 0.5 | VOD lane on the MacBook (chat -> detector -> 30 s downloads -> template -> Telegram) | 5 approved clips from one real VOD |
| 1 | watcher, recorder, chatlog, systemd, heartbeats, disk cleanup, alerts | 3 nights of complete recordings unattended |
| 2 | detector confirm stage, viewer-clip polling, audio check, replay tests vs answer key | >=60% top candidates good |
| 3 | transcription, facecam calibration, template, captions, hook | every render passes ffprobe; 10 samples liked |
| 4 | Claude screen/judge, Telegram approval, DRY_RUN test channel | 1 week, >=50% worth posting |
| 5 | YouTube, Instagram, TikTok inbox, campaign link collection | 7 days scheduled posts, no errors |
| 6 | weekly compilation + voiceover, analytics, learning loop, Kick adapter | first compilation monetized |

Status (17 Nov 2026, evening):
- Done: VOD lane (Twitch), live lane `python -m clipwatcher.live` (record + anonymous chat + detect + cut +
  captions + per-clip layout + render + stage + Telegram card) verified on real Twitch and Kick streams;
  Docker image for a NAS.
- Telegram: bot @clippersosabot in the group (chat id in config/operators.yaml, token in .env); approval cards
  post with video; Claim/Approve/Reject/Edit/Release work for the owner. Manu/Samu IDs still 0: they must
  join the group and send a message, then read their IDs (getUpdates or getChatAdministrators).
- Branding: ClipVault (youtube.com/@clipvault4.0). Pill banners from assets/branding/src via
  scripts/build_brand_assets.py: top banner @y96, SUBSCRIBE/LIKE/COMMENT banner @y1300, subs at y1495.
- Lessons from real runs: small chats need an adaptive unique-chatter floor; bots posting every few seconds
  must not hold fires; live fires must mature ~15 s to get their real peak score; faster-whisper must not go
  through PyAV; each clip needs its own layout detection; Kick chat = web.kick.com history polling (skip the
  first backlog page); pruning must never stop on low disk (it filled the Mac twice); resolve streamlink next
  to the venv Python; kill recorders by process group.
- Running on the Mac (detached, nohup + caffeinate): `clipwatcher.live` on illojuan, peereira7, teegrizzley,
  officermesser, paulinholokobr, kenetgr, minik for 480 min (log work/live_mix7.log) and
  `clipwatcher.queue.telegram_bot` (log work/telegram_bot.log). Stop with `pkill -TERM -f clipwatcher`.
- Before launch (19 Nov): Manu/Samu IDs, streamer list for launch night, a host that stays on, maybe
  fastlane.min_score 4 for more choice, a FOLLOW banner for IG/TikTok.
- Next: Claude judge (ANTHROPIC_API_KEY; hook is the first spoken words until then), publishing (YouTube/IG
  creds; uploader not running, DRY_RUN=1), viewer-clip confirmation, ratedpicz Kick slug.
