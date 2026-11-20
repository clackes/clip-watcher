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

Status (17 Nov 2026, night). Handoff and next steps: docs/HANDOFF.md.
- Done: VOD lane (Twitch), live lane `python -m clipwatcher.live` (record + anonymous chat + detect + cut +
  captions + per-clip layout + render + stage + Telegram card) verified on real Twitch and Kick streams;
  Docker image for a NAS.
- Telegram: bot @clippersosabot in the group (chat id in config/operators.yaml, token in .env); approval cards
  post with video (sent with width/height/duration + thumbnail, so iOS no longer stretches 9:16; owner
  confirmed on iPhone); card shows title, hook, judge score, flags, description. Claim/Approve/Reject/Edit/
  Release work for the owner. `/id` in the group replies with a member's Telegram ID; Manu/Samu still 0.
- Branding: ClipVault (youtube.com/@clipvault4.0). Since 18 Nov the Short is the split layout (CLAUDE.md
  D11): webcam over gameplay, small ClipVault logo, one caption word at a time on the seam, no CTA banner.
  The SUBSCRIBE/LIKE/COMMENT banner was removed on 17 Nov (D5). The older stack template (top banner from
  assets/branding/src via scripts/build_brand_assets.py, subs at y1495) is kept as `render.layout: stack`.
- Lessons from real runs: small chats need an adaptive unique-chatter floor; bots posting every few seconds
  must not hold fires; live fires must mature ~15 s to get their real peak score; faster-whisper must not go
  through PyAV; each clip needs its own layout detection; Kick chat = its realtime websocket, the chat history
  only for gaps and with a fresh cursor (the bare address is cached 10 s), time from created_at and never from
  message_ref, at most 1.5 requests a second (CLAUDE.md D15); pruning must never stop on low disk (it filled the Mac twice); resolve streamlink next
  to the venv Python; kill recorders by process group.
- Keywords (D16, 20 Nov): streams have kinds (`categories:` in global.yaml, GTA VI first) and streamers can
  have learned words (config/keywords.yaml), but 403 h of GTA VI chat showed that more words do not find
  more of what viewers clip: eight laughter words carry almost all a keyword list adds (78 clips found of
  653 with none, 87 with the eight, 90 with the list we have), and chat volume carries the rest. To find
  more moments, look outside the word list. `scripts/learn_keywords.py learn --validate` is the test bench.
- Judge v2 (built, not yet run against the API): static cached prompt prompts/judge_v2.md, 8 labelled
  480x270 frames, compact JSON in/out, clickbait title/description rules, spoiler/music/gambling flags that
  reject a clip. Live lane calls it once per clip when ANTHROPIC_API_KEY is set and trims to its t0/t1.
  Simulated benchmark (docs/JUDGE_BENCHMARK.md): ~$0.0044/clip on Sonnet 5.5 (was ~$0.015), $0.23 for 50
  clips, ~135k clips on $600. Config: judge.* in config/global.yaml (mode single, Sonnet).
- Nothing is running: the live test and the Telegram bot were stopped on 17 Nov. Restart commands in
  docs/HANDOFF.md.
- Before launch (19 Nov): Manu/Samu IDs, streamer list for launch night, a host that stays on, maybe
  fastlane.min_score 4 for more choice, a FOLLOW banner for IG/TikTok.
- YouTube: Approve uploads at once; nothing is published while DRY_RUN=1. Default
  `publish.youtube_mode: browser` (real Chrome through YouTube Studio; sign in once with
  scripts/yt_browser_login.py), `api` (Data API token from scripts/youtube_auth.py) for a server without a
  display. Details: docs/HANDOFF.md, next step 2.
- Queue (19 Nov): an expired claim goes back to the Claim buttons; a clip gets one card and the approval
  bot posts the ones that went missing; a failed upload shows on the card with Retry; staging a clip id
  again only replaces a clip that is still READY.
- Companion app (20 Nov, D17): `python -m clipwatcher.ui --open`. Runs the live lane, the VOD lane and the
  tests from one page with live progress; staged clips with their score make-up and per-video caption
  settings (render again); speed, judge cost, judge decisions and detector charts; settings saved for this
  machine in config/local.yaml. To come with it: real judge numbers once an API key is set (the cost page
  estimates until then).
- Animated captions (20 Nov, D19): 25 Remotion templates as `rm:<template>` caption styles, the built-in
  styles as default and fallback; in the companion app's library with live previews, every parameter of a
  template to fine-tune, and per-video template, colours and settings. Needs `npm ci && npm run setup` in
  remotion/ once per machine. To decide: templates in the live lane (about half a clip's length of extra
  render) and the Remotion company licence if the team grows past three.
- Next: ANTHROPIC_API_KEY -> `scripts/judge_cost_benchmark.py BENCH --exact` and real judge calls on staged
  clips (until then the hook is the heuristic), publishing (YouTube/IG creds; DRY_RUN=1), viewer-clip
  confirmation with Twitch credentials, minik's language.
