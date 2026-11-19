# Clip Watcher

24/7 Python bot: watches Twitch/Kick streams, finds funny/hype moments from chat + audio + Claude judging,
renders a branded 9:16 Short + clean 16:9 clip, stages them in a Telegram approval queue for 3 operators
(owner, Manu, Samu). Spec: "Clip Watcher Blueprint v2.1". Roadmap: docs/ROADMAP.md.

## Base rules
- `DRY_RUN=1` is the default (`config.dry_run()`). Tests never touch the network and never upload.
- The publisher uploads only clips in state APPROVED. `publish.auto_approve` (default false) lets the bot
  approve top-scored clips itself.
- Credit the streamer on screen (`twitch.tv/<name>` tag) and in metadata. Campaign link lives in streamers.yaml.
- Do not add content filters or blocking rules the spec doesn't ask for.
- Secrets via env / credential files only, never committed. No model names in commits or code comments
  (model IDs live only in config/global.yaml).
- ffmpeg: always resolve with `config.ffmpeg_bin()` / `config.ffprobe_bin()` (prefers ffmpeg-full; the lean
  Homebrew ffmpeg has no libass).
- Work only on `main`. Small commits.

## Dev
```
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q            # -m "not ffmpeg" to skip render tests; 2 tests skip without numpy / streamlink
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## Testing against reality
- Replay real VOD chats through `LiveDetector` and compare with batch `detect()` top-10 (see git log
  81afd37). Keep >= 8/10 matches on clix/zackrawrr/tfue/kaicenat VODs when changing the detector.
  `python scripts/replay_chat.py --vod <id> --login <channel>` does it: downloads the chat replay, replays it
  as the live lane would, prints clips/h, the top-10 match and how many of the channel's most viewed
  viewer-made clips the detector fired on (the ground truth D13 was tuned against; old detector 18/180).
- `python scripts/learn_keywords.py learn --category "<name on Twitch>" --validate` tests a keyword list (and
  any change to how keywords count) on the corpus in work/corpus/: viewer clips found at a fixed number of
  clips an hour, on clips nothing was learned from (D16; GTA VI: 653 clips in 36 channels, global list 90).
- `scripts/force_clip.py` pushes one forced moment through the live pipeline on a running ring buffer.

## Decision log (v2.1, binding)
- D1 Ingestion: streamlink -> .ts + anonymous Twitch IRC TLS (irc.chat.twitch.tv:6697) + Kick's realtime
  websocket (D15; Pusher is gone). patchright only as optional Kick Cloudflare cookie adapter, never video
  capture.
- D2 Ring buffer: 10 s .ts segments, 15-60 min window (default 30). Candidates pin window +/-60 s. Pins expire
  after render or 48 h. Recording pauses and alerts below 15% and 5 GB free; pruning never stops. One ring
  folder per streamer for the life of the process: a lane restart records into it again (clips in flight keep
  their pinned video), only folders of earlier processes are swept at start.
- D3 Spike trigger: chat_score = 0.5*z(U) + 0.35*z(E) + 0.15*z(S), median/MAD over 15 min. E uses
  config/global.yaml keywords merged with per-streamer extras. Bot/spam/giveaway/raid filters. Confirm with
  viewer clips + audio loudness. Formula, baseline and gate are superseded by D13 (the fixed final >= 4.5 too);
  window, keywords, filters and the 8 s chat delay stand. Confirmation since 19 Nov: `confirm()` adds +1.5 for
  a viewer clip within 30 s and +0.3 per loudness z (max 1.5) to final_score, shown in meta (loud_z,
  viewer_clip). Live: loudness of the cut window always; viewer clips through Helix when TWITCH_CLIENT_ID /
  TWITCH_CLIENT_SECRET are set, and a viewer clip then also rescues a spike that fired under the keep gate in
  the last `detector.rescue_s` (600). VOD: the channel's 100 most viewed clips of the week (no login) reorder
  twice as many candidates.
- D4 Dual export: clean_16x9.mp4 (trimmed, no overlays, -14 LUFS) + short_9x16.mp4. Profiles cpu_batch,
  cpu_live, nvenc, mac_preview.
- D5 One 9:16 template (1080x1920; owner 17 Nov: no CTA banner, stack moved down 125 px, see template.STACK_SHIFT):
  bg | ClipVault top banner 1000x166 centred @y221 | webcam 1080x480 @y395 | hook pill
  y827 t<render.hook_s (3.5s) | gameplay 1080x540 @y875 | (CTA banner removed) | subs \pos(540,1495) 80px current word #FFE600
  110% (owner's call 17 Nov; replaced the same day by `render.captions: pop`: Anton (assets/fonts, OFL), up to 3
  words per line, white, spoken word yellow and pops 100->135->125% (owner: one colour only); classic/bounce/ab
  kept as options, choice in meta.captions. Cold open (owner, 17 Nov): the strongest reaction plays first (~2 s,
  facecam punch-in, captioned), then the clip from its start. Reaction = judge pk; without the judge a shout
  (>= 8 dB over the streamer's talking level + an exclamation word, render/cold_open.py), else no cold open; banners from assets/branding/src via
  scripts/build_brand_assets.py) | dead zone 1536-1920. Credit {\an9\pos(1060,407)}. Background ambient blur or
  frame_gradient_<accent>.png (-loop 1); accent red/yellow/cyan from hash(clip_id). No webcam -> 16:9 frame
  1080x608 centred in y395-1415. Cold open: webcam only (centred on the face) filling y395-1415, zoompan punch-in.
- D11 Split Short (owner, 17-18 Nov; supersedes the D5 stack, kept as `render.layout: stack`): webcam 1080x960
  over gameplay 1080x960, small ClipVault logo, no on-screen hook, no CTA banner. Captions `word`: one word at a
  time on the seam (y960), Anton, shouted words yellow. Judge `fx` beats (hype|fail|shock|funny + label like
  *FAIL*) -> zoom punch, colour effect, coloured label and a meme sound from assets/sfx/<kind>/ (myinstants US
  trending, owner's pick; some may get audio claims). No webcam -> 1080x760 frame over a blurred copy.
- D12 Edit like the top clip channels (owner, 18 Nov; from 20 Shorts with 0.1-130M views): open on the setup
  line, end on the punchline (cold open off, `render.cold_open_s: 0`); jump cuts drop pauses between lines
  (render/jumpcut.py: 0.8-4 s pauses, never below `render.clip_min_s` / 60 %, cut edges on the frame grid so
  picture and sound stay together, beats and action labels protected); judge `ac`
  action labels (*WATCHING*) on silent stretches; labels and sounds only in pauses (template.cues); no-webcam
  full-camera / IRL clips reframed to a full-screen 9:16 window that follows the face (render/reframe.py).
  Judge: thinking on at low effort (off, the same clip scored 5-8), a frame every 2.5 s (8-16), audio peaks `au`.
- D13 Whole-stream chat baseline (owner, 19 Nov: keep the whole chat, read it from the start, baseline from
  all of it, count message volume and not only keywords; supersedes the 15 min median/MAD, the weights and
  the fixed gate of D3; the level blend and gate quantiles were tuned on real chats, see below). The chat of
  a broadcast is kept whole in work/chat/<login>/<stream id>.jsonl and never pruned; only the video stays a
  rolling window. On joining (start, restart, crash) `session.StreamSession` reads the
  saved log, fetches the rest from Twitch's chat replay from the first second of the stream (anonymous GQL by
  VOD offset, `chat/twitch_replay.py`; up to `chat.backfill_max_h` 12; needs VODs on; Kick: its chat
  history, D15),
  primes the detector with it and holds live chat meanwhile; a new broadcast gets a new log and detector.
  `detect/session.SessionTable` is the stream as a table, one row (U, M, E, S) per second:
  chat_score = 0.3*z(U) + 0.25*z(M messages) + 0.3*z(E) + 0.15*z(S). Level = half whole-stream median, half
  last-60-min median; scale = MAD of every second of the stream around its level. Gate once 15 min of history
  exist: fire in the top 2% of the stream's seconds, clip when the peak reaches the top 1% (`fire_q`/`keep_q`;
  fixed fire_z 3 / keep_z 5 before that; `max_per_hour` 8). Keywords also match held keys (LMAOOO, WWWW).
  The cooldown starts at kept spikes only and a spike 1.5x the last kept one overrides it. Candidates carry
  session_pct / session_rank (meta chat_pct / chat_rank). Replay of 6 real chats (55 h) against 180 viewer
  clips: 34 hit at 3.1 clips/h (before: 18 at 2.2/h, none on the two biggest chats).
- D15 Kick (checked against the live service 19 Nov, all without login or key; `chat/kick.py`). Live chat:
  POST web.kick.com/api/v1/realtime/channels/<channel id>/chat/connection names a Centrifugo websocket, POST
  .../realtime/auth/connection gives a guest token (45 min, renewed on the open connection); messages come on
  chatrooms.<chatroom id>.v2 as App\Events\ChatMessageEvent, complete, ~0.5 s late; gifted subs (5+) and
  hosts mute like sub bombs and raids. History: GET kick.com/api/v2/channels/<channel id>/messages?cursor=<epoch
  microseconds> = the 25 messages before that instant, any instant, VODs 17 days old included; it fills the
  gap after a reconnect, is the polling fallback when the websocket cannot be opened, backfills a broadcast
  and is the VOD lane's chat. Always with a cursor: the bare address is cached 10 s. Kick lets about 1.6
  requests a second through (kick.com and web.kick.com together) and answers 429 beyond, so all requests share
  one limiter (`kick.max_rps` 1.5) and history comes at ~35 messages a second: a broadcast is backfilled
  newest first for `chat.backfill_first_s`, detection starts, the rest is read in the background and the
  detector rebuilt on it every `chat.rebuild_every_s`. Time is the server's created_at (whole seconds);
  metadata.message_ref is the viewer's clock (6% off by more than 5 s, some 40 s ahead) and only orders
  messages inside their second. Broadcast id and start: /api/v2/channels/<slug> (livestream). Viewer clips:
  /api/v2/channels/<slug>/clips, public, so D3's clip confirmation needs no credentials on Kick; a moment is
  confirmed when at least twice the usual number of clips cover it (big channels clip every few seconds).
  VODs: /api/v2/channels/<slug>/videos gives start, length and the HLS playlist, cut by ffmpeg
  (/api/v1/video/<uuid>, which yt-dlp asks, answers 403).
- D16 Keywords per kind of stream and per streamer (owner, 19 Nov: the tool is customizable per live type,
  GTA 6 first, and per streamer). A stream's category as its platform names it (Twitch `stream.game.name`,
  Kick `livestream.categories[0].name`) picks a profile under `categories:` in global.yaml by its `match`
  texts ("grand theft auto vi" is in Twitch's "Grand Theft Auto VI" and Kick's "Grand Theft Auto VI (GTA)");
  a profile holds `keywords` and any `detector` setting. `config/keywords.yaml` (absent until a word earns
  its place) holds learned words per category and per streamer. `config.keywords_for(name, category)`: global
  list, learned category words, the profile's hand-set ones, learned streamer words, the streamer's hand-set
  `keywords`; hand-set weights win, a learned one only adds a word or raises it, KEKW is kekw.
  `StreamSession.category` follows the platform; on a change of game the detector takes the new words and
  keeps its baseline (`LiveDetector.set_category`); VOD lanes: `--category`, else what the platform says.
  `scripts/learn_keywords.py`: `collect` (chat replays of the category's biggest channels, cut to the
  category by Twitch's chapter markers, with their viewer clips of the last day/week/month; `--streamers`
  for our own channels in any category; no login; work/corpus/), `learn` (a word or pair is the category's
  when chatters type it >= 2x more around viewer clips than otherwise, in >= 15 moments and >= 20% of the
  channels, never mostly from one moment; a streamer's when it does so >= 3x in that one chat and is not
  plain talk elsewhere), `--validate` (the real batch detector with and without the words on the half of
  each channel's viewer clips no word was learned from, category words learned from other channels),
  `--write` (only what found >= 5% and >= 3 more clips; `--force` otherwise). First run 20 Nov, GTA VI: 49
  channels, 403 h, 3.78 M messages, 1309 viewer clips in 34 English chats. Result: GTA VI chat reacts with
  the global list's words (the learner found 15 of them on its own), plus damn, crazy, ayo, gyat, jesus,
  "that was", "oh my" and words of the game's own scenes (wasted, physics, jiggle, banned, coffee, psycho).
  Adding them found no more viewer clips: 90 of 653 with and without at 3 clips/h, 126 vs 129 at 6/h; 79
  when the detector's own spikes were moments too, 85 when learned on the test clips themselves; streamer
  words 101 vs 100. So nothing is written and the GTA VI profile has no keywords of its own. Measured on
  the way: with no keyword at all the detector finds 78 of the 653, with eight laughter words 87, with the
  global list 90; D13's weights were at the top of eight alternatives on 1327 clips (183; best 187, not in
  both halves of the channels). Dropped as labels: spikes of message volume alone (mostly chat answering the
  streamer: "yes" and "no" lead, viewers clipped none) and counting single words' bursts on clips (too few
  per word).
- D14 Clips run 30-59 s (owner, 19 Nov, as read from "at least 30 seconds up to 59"; replaces the 12-30 s of
  D6/D12; `render.clip_min_s: 12` brings the short clips back): the candidate window is 59 s of
  footage (`detector.before_s` 40 / `after_s` 19, same in `vod`), the judge trims inside 30-59 s (prompt) and
  `snap.fit_length` enforces it in code (too short: start earlier, then end later; too long: the oldest part
  goes). Jump cuts never go below `render.clip_min_s`.
- D6 Judge (v2, 17 Nov): one static cached system prompt (prompts/judge_v2.md, no per-clip variables) + 8
  labelled 480x270 JPEG q65 frames + compact JSON {meta,tr,ch,task}; short-key output {ok,sc,t0,t1,hk,tt,ds,
  fl{sp,mu,gb},why}, caps/credit enforced in code (judge/compact.py). Thinking off, effort low (judge.* in
  global.yaml). judge.mode single | two_stage | off. Owner asked for spoiler/music/gambling flags; sp/mu
  clips are rejected (judge.reject_flags) in every lane, before rendering (live, VOD, batch); gb is informational since 17 Nov (gambling streamers are clipped). Heuristic fallback on API errors (English fallback copy, same shape). Judge t0/t1 start late (<= 2 s before the
  action; fallback 10 s before the reaction), never cut a word, end on a finished sentence (judge/snap.py).
- D10 English only (owner, 17 Nov): `channel.language: en`. All copy (hook, title, description, fallbacks) is
  English; streamers whose `language` differs are skipped by live/vod. Description = 4 lines: teaser + credit,
  a clip-specific question ending in 👇, a reason to follow (no "like/comment/follow" begging, stripped in
  code by `compact.is_bait`), hashtags. Cost: scripts/judge_cost_benchmark.py.
- D7 Telegram queue with atomic SQLite claim lock (15 min TTL), audit log, staging/<clip_id>/, staging/approved/,
  staging/published_clips.json. One staging folder for lanes, bot and publishers: `staging.staging_root()`
  (CLIPWATCHER_STAGING, else <repo>/staging). Since 19 Nov: a claim that ran out goes back to READY (bot sweep
  every minute, the card shows the Claim buttons again); staging an id again only refreshes a clip that is
  still READY (a decided clip keeps its state and video); every READY clip gets a card: the lane posts it, the
  bot posts the ones that are missing (VOD and batch clips too, `publish.card_max_age_h`).
- D8 Publishing: YouTube Data API + Instagram Graph API from server, APPROVED only. TikTok/campaign forms by a
  human with Claude in Chrome from staging/approved. `publish.youtube_mode` browser (Chrome via patchright,
  default) | api; CLIPWATCHER_YOUTUBE_MODE overrides it (the Docker image sets api: no display). Since 19 Nov:
  the upload starts whatever Telegram does, a failure shows on the card with a Retry button, a video id is
  recorded as soon as YouTube gives one, and a restarted bot uploads what was approved in the last
  `publish.resume_max_h` and never got there.
- D9 .ts seek bug: never `-ss` exact on MPEG-TS. Coarse input seek to max(0, START-6), then
  trim/atrim=start=TRIM in the graph, then setpts=PTS-STARTPTS. Regression test required. Commands that decode
  only the picture or only the sound (judge frames, transcript, loudness, reframe) go through
  `render/seek.one_stream` (-copyts + the file's start time): ffmpeg's zero differs there by the file's start
  time, 1.4 s on a ring-buffer cut. Every render is checked afterwards (streams present, length as asked).

## Module map and interfaces (keep signatures stable)
- `models.py` ChatMessage, Candidate, JudgeResult, Word. `config.py` loaders + tool paths.
  `config.keywords_for(name, category=None)`, `category_profile(category) -> (kind, settings)`, `learned()` (D16).
- `chat/convert.py` `tdl_to_messages(path) -> list[ChatMessage]`, `read_jsonl/write_jsonl`.
- `detect/detector.py` `detect(messages, *, streamer, viewers=None, top, before_s, after_s, gap_s, category=None) -> list[Candidate]`
  (whole chat known: scored against all of it), `settings(streamer, category=None)`, `gate(table, settings)`.
  `detect/session.py` `SessionTable` (D13: per-second rows, level/scale/gate statistics).
  `detect/baseline.py` `x_normal_series(messages, bucket_s) / x_normal_top(...)`.
- `render/template.py` `render_short(src, start_s, dur_s, out, *, clip_id, streamer, words, hook,
  platform="youtube", style=None, accent=None, cam=None, game=None, has_webcam=True, profile="cpu_batch") -> Path`
  and `render_clean(src, start_s, dur_s, out, *, profile="cpu_batch") -> Path`. `render/captions.py` ASS writer.
  CLI `python -m clipwatcher.render`.
- `judge/transcribe.py` `transcribe(media, start_s, dur_s, language) -> list[Word]` (faster-whisper optional).
  `judge/frames.py` `keyframes(media, start_s, dur_s, n=6, height=480) -> list[Path]`.
  `judge/claude.py` `screen(...)` (legacy), `judge_candidate(cand, words, chat_lines, frames, *, title, game,
  client=None) -> JudgeResult` (now runs v2) and `judge_clip(cand, words, chat_lines, [(t, jpg)], *, game,
  platform, language, task)`. `judge/compact.py` serializers + schema; `judge/frames.py keyframes_lowres`.
- `queue/db.py` ClipDB (SQLite: clips, jobs, audit; atomic claim, card tracking). `queue/staging.py`
  `staging_root()`, `stage_package(clip_id, short, clean, meta) -> Path`, `mark_approved(...)`.
  `queue/telegram_bot.py` `post_card(app, db, clip_id, video) -> bool`, `post_missing_cards`, `sweep`.
- `publish/youtube.py` behind DRY_RUN.
- `vod.py` Day-0 VOD lane CLI `python -m clipwatcher.vod`.
- Live ingest (`ingest/`): `ringbuffer.RingBuffer(root, window_min, segment_s)` with `add_segment(path, pdt_ms)`,
  `pin(start_ms, end_ms, ttl_s)`, `prune()`, `cut(start_ms, end_ms, out) -> Path` (wall-clock epoch ms via
  #EXT-X-PROGRAM-DATE-TIME map). `watcher.py` live polling (Helix / Kick), `recorder.py` streamlink supervisor.
- Live chat (`chat/twitch_irc.py`, `chat/kick.py`): async iterators yielding ChatMessage with epoch-ms ts.
  `chat/kick.py` `KickChat(slug)` (websocket + gap fill + polling fallback), `live_session(slug)`,
  `replay(channel_id, start_ms, end_ms, budget_s=None) -> (messages, instant reached)`, `message_to_chat`.
  `chat/twitch_replay.py` `live_session(login) -> Session | None` (stream id, start, category), `vod_category(vod_id)`,
  `fetch(vod_id, start_s, end_s) -> list[ChatMessage]`
  (chat from before the bot joined). `chat/live_log.py` `LiveLog(login, stream_id, root)` `.write/.read/.rewrite`.
  `session.py` `StreamSession(login, root=, platform=)`: `.feed(msg)` like a detector, `.watch()` task (D13).
  `detect/live.py` `LiveDetector(streamer, viewers=None, category=None).feed(msg) -> Candidate | None` (epoch
  seconds), `.set_category(category)` (D16),
  `.prime(history) -> int` loads earlier chat as baseline without emitting clips.
  `detect/viewer_clips.py` Helix Get Clips poller (live), `vod_clip_moments(login, vod_id)` (VOD, no login);
  `detect/detector.py` `confirm(candidate, viewer_clip_times, loud_z)`, `loud_z(series, peak_rel)`.
- `fastlane.py` live lane orchestrator; `calibrate.py` facecam/gameplay rect suggestion + Telegram still.
- `layout.py` `resolve(media, streamer) -> Layout`: per-clip scene detection. Streamers use different layouts and
  switch scenes mid-stream, so each clip matches its face position against confirmed `layouts:` in
  streamers.yaml; unknown scenes render with an auto rect and meta.layout="auto-new" for operator review.
