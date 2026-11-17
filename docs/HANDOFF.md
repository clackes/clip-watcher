# Handoff (17 Nov 2026)

Everything needed to continue is in this repo (github.com/clackes/clip-watcher, branch main) plus three
local files that are gitignored on purpose. Status and lessons: docs/ROADMAP.md. Rules: CLAUDE.md.

## Local files NOT in git (keep a copy in a password manager)
- `.env`: `DRY_RUN=1`, `TELEGRAM_BOT_TOKEN` (bot @clippersosabot; can be re-issued in @BotFather with /token).
- `config/operators.yaml`: `telegram_chat_id: -5341226680`; owner telegram_id 856759682; manu/samu still 0.
- `config/streamers.yaml`: restore from `config/streamers.backup.yaml` (committed copy, no secrets).
- Rebuildable, not needed: `staging/` (test clips), `work/` (logs, ring buffers), `models/` (face model,
  downloaded again by the Dockerfile), `.venv/`.

## Running right now on the Mac (detached; survives closing Claude)
- `clipwatcher.live` on 7 test channels, 480 min, log `work/live_mix7.log` (stops by itself).
- `clipwatcher.queue.telegram_bot` (runs until stopped), log `work/telegram_bot.log`.
- Stop all: `pkill -TERM -f clipwatcher`. Restart:
  `set -a; . ./.env; set +a; nohup .venv/bin/python -m clipwatcher.queue.telegram_bot > work/telegram_bot.log 2>&1 &`

## Flow the owner wants (17 Nov)
Clip ready -> Telegram card with video at once -> owner approves ASAP -> publish to YouTube/Instagram/TikTok.
- Done: card on ready; Approve uploads to YouTube immediately (`publish.youtube_on_approve`) and replies with
  the link. Tested only in dry run (no YouTube credentials yet).

## Next steps, in order
1. Samu types `/id` in the group; put his ID in config/operators.yaml (same for Manu).
2. YouTube credentials (owner signs in): Google Cloud project "ClipVault" -> enable YouTube Data API v3 ->
   OAuth consent screen External, publish to Production (Testing tokens expire in 7 days) -> OAuth client
   "Desktop app" -> save as `secrets/client_secret.json` -> run
   `.venv/bin/python scripts/youtube_auth.py secrets/client_secret.json secrets/youtube_token.json`
   (pick the ClipVault channel) -> add `YOUTUBE_CREDENTIALS=secrets/youtube_token.json` to .env.
   Expect: unaudited API projects upload as private (flip to Public in YouTube Studio) and ~6 uploads/day
   quota; apply for the YouTube API audit for public uploads + more quota.
3. ANTHROPIC_API_KEY into .env, then test the Claude judge on real clips (never run against the API yet).
4. Launch-night streamer list from the owner -> config/streamers.yaml (platform, has_webcam, layouts).
5. NAS deploy: need brand/model, CPU arch, Docker support, free disk. Then
   `docker compose -f deploy/docker/compose.yaml up -d --build` with .env, operators.yaml, streamers.yaml and
   secrets/ copied over. Consider fastlane.min_score 4 for launch night.
6. Instagram: Business/Creator account linked to a Facebook Page, Meta app + long-lived token; Graph API pulls
   the video from a public URL, so approved clips need to be served (e.g. tunnel from the NAS).
7. TikTok: unaudited Content Posting API posts private only; for launch, save the video from the Telegram card
   and post from the phone (spec D8: human step).
