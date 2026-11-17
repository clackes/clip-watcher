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

Status (17 Nov 2026):
- Done: VOD lane (Twitch; Kick untested), live lane `python -m clipwatcher.live` (record + anonymous chat +
  detect + cut + captions + layout + render + stage) verified on real live streams; Docker image for a NAS.
- Lessons from real runs: small chats need an adaptive unique-chatter floor; bots posting every few seconds
  must not hold fires; live fires must mature ~15 s to get their real peak score; faster-whisper must not go
  through PyAV; each clip needs its own layout detection.
- Before launch: Telegram bot token + operator IDs, streamer list for 19 Nov, a host that stays on.
- Next: Claude judge (API key), publishing (YouTube/IG creds), Kick live test, viewer-clip confirmation.
