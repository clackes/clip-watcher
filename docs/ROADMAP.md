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

Status: M0 scaffold + M1 part 0 (VOD lane) and part 1D (render/queue/publisher) in progress.
Next: M1 part 1A (watcher/recorder/ring buffer) and 1B (live IRC/Kick chat adapters).
