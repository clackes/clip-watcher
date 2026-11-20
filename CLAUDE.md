# Clip Watcher

24/7 Python bot: watches Twitch/Kick streams, finds funny/hype moments from chat + audio + Claude judging,
renders a branded 9:16 Short + clean 16:9 clip, stages them in a Telegram approval queue for 3 operators
(owner, Manu, Samu). Spec: "Clip Watcher Blueprint v2.1". Roadmap: docs/ROADMAP.md.

## Base rules
- `DRY_RUN=1` is the code default (`config.dry_run()`); the server runs DRY_RUN=0 since 19 Nov (owner): Publish in
  Telegram uploads public. Tests never touch the network and never upload.
- The publisher uploads only clips in state APPROVED. Owner, 19 Nov (later): the channel posts by itself,
  `publish.schedule` (queue/schedule.py): at most 6 Shorts in any 24 h, >= 3 h apart, the best judge-scored
  READY clip (>= 6, < 24 h old, never heuristic-only); operator Publish still works and counts toward the cap.
  A failed upload puts the clip back and pauses 1 h. Telegram's Redo (optional note) re-cuts a clip.
- The channel is GTA 6 clips from many streamers (owner, 19 Nov): `channel.games` (ingest/category.py,
  streamlink --json metadata): a lane records and reads chat only while the stream is in that category
  (`live.wait_for_game`, re-checked every 5 min: a switch or 15 min offline stops it), and moments from any
  other category are dropped before the cut. One template for all. Owner, 19 Nov 15:40: schedule off, YouTube only after an operator approves in Telegram.
- Credit the streamer in metadata (title/description). Owner, 18 Nov: no on-screen `twitch.tv/<name>` tag
  (`render.credit_on_screen: false`). Campaign link lives in streamers.yaml.
- Do not add content filters or blocking rules the spec doesn't ask for.
- Secrets via env / credential files only, never committed. No model names in commits or code comments
  (model IDs live only in config/global.yaml).
- ffmpeg: always resolve with `config.ffmpeg_bin()` / `config.ffprobe_bin()` (prefers ffmpeg-full; the lean
  Homebrew ffmpeg has no libass).
- Work only on `main`. Small commits.

## Dev
Code is edited and committed on the Mac (`main`); the bot runs only on the Windows Server 2025 box
140.82.40.80 (`C:\clip-watcher`, since 19 Nov; the 18 Nov box 207.148.28.168 is stopped), driven from the Mac over WinRM. No GitHub and no SSH (owner's choice).
The Mac has no venv (space): tests run on the server.
```
cwr() { uv run -q --with pywinrm==0.5.0 scripts/remote/cw_remote.py "$@"; }   # Keychain "clipwatcher-winrm-<host>"
cwr push            # commit first; sends the server's missing commits as a git bundle
cwr test            # pytest on the server (default -m "not ffmpeg"; pass args for more)
cwr test -m remotion   # the caption frame tests (D19): only where remotion/ is set up, ~45 min
cwr restart [xqc,lacy|all]   # restart the bot (new code); a list is saved in config\live_streamers.txt
cwr status ; cwr logs -n 80 -g 'judge|ERROR' ; cwr stop ; cwr put/get <file> ; cwr ps script.ps1
cwr asuser script.ps1   # in a password-logon task: the only way to touch the YouTube Chrome profile
cwr bootstrap --freeze freeze.txt   # set up a fresh server
uvx ruff check . && uvx ruff format --check .
```
- On the server: scheduled task `cw-live` runs `scripts\run_live_windows.ps1` (loads .env, restarts a crashed
  lane/Telegram bot). Since 18 Nov it runs logged on or not, starts at boot, no window (`cwr install`, password
  logon: a Remote Desktop sign-out killed the old interactive task). Only one Telegram bot per token may poll:
  never start a second copy.
- `config/streamers.yaml` / `operators.yaml` are not in git: edit on the Mac, `cwr put` them to `C:\clip-watcher\config\`.
- Remote desktop (YouTube login, watching windows): `scripts/remote/clipwatcher-server.rdp` in Windows App.
  YouTube login: `run_live_windows.ps1 -YtLogin -Account <username>` on the server desktop.
- Logs: `work\logs\live-<yyyymmdd>.log` (UTF-8). ffmpeg 8.1 (libass) in `C:\ffmpeg\bin` via CLIPWATCHER_FFMPEG.
- Never open the YouTube Chrome profile over WinRM (network logon): Chrome replaces its cookie key and signs out.
- YouTube uploads use the patchright Chrome profile `~\.clipwatcher\yt-profiles\<YT_BROWSER_ACCOUNT>` (capybarasking).
- Windows needs the VC++ runtime for Whisper (installed). Mac/Linux: `.venv/bin/...` equivalents still work.

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
  Owner, 18 Nov: a spike also needs `detector.min_reaction_share` (0.15) of real reactions (keyword weight
  >= 1.5, laughter, clip link), and only a sub-bomb of `detector.sub_bomb_min` (10) gifts mutes (both kept on
  top of D13, batch and live).
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
  global.yaml). judge.mode single | two_stage | off. Owner asked for spoiler/music/gambling flags; sp
  clips are rejected (judge.reject_flags) in every lane, before rendering (live, VOD, batch); gb (17 Nov) and mu (18 Nov, owner accepts music-claim risk) are informational. Content (sexual, violent,
  crude, "ad-unsafe") is never a minus by itself (owner, 18 Nov). Heuristic fallback on API errors (English fallback copy, same shape). A still edge ("NO SIGNAL", frozen/black screen) is cut (render/still.py). Judge t0/t1 start late (<= 2 s before the
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
- D17 Companion app (owner, 20 Nov: one small UI to run the whole suite and see live progress, history,
  configuration, caption templates per video, API cost, speed, judge history and stats; the design was
  approved as an artifact first: ClipVision's own typography, colours, gradient and logo, one thing per
  screen, a step-by-step run). `python -m clipwatcher.ui` (127.0.0.1:8765, `--open`): a standard-library
  HTTP server, no new dependency; the page, its fonts (Geologica, OFL) and Chart.js are in `ui/static/`, so
  it works offline. Four places: Run (Choose > Source > Captions > Run > Result; live lane, VOD lane, test
  suite), Clips (every staged clip, its video, how it scored, settings for this video), Insights (speed,
  cost, judge, detector: one number and one chart at a time), Settings. It starts the same commands a person
  would type, one at a time, as subprocesses (`ui/runs.py`) and shows what they report: lanes write progress
  events to the JSONL named by CLIPWATCHER_EVENTS (`events.py`: recording, baseline, watching, moment, step
  start/done/failed, judged, staged, rejected, failed), sent on to the page as server-sent events. It reads
  what the bot already keeps: the staging folder and queue, each clip's meta (`timings`, `signals`), the
  judge ledger (`judge/ledger.py`, work/usage/judge.jsonl: tokens and USD by kind of token at the day's
  `judge.prices`). Settings go to config/local.yaml (not committed, laid over global.yaml,
  CLIPWATCHER_LOCAL=0 ignores it), never into global.yaml. "Clip the last 45 seconds" is
  scripts/force_clip.py on the run's ring. Render again: lanes keep `recipe.json` beside each staged clip
  (words, webcam and gameplay rectangles, beats; `render/recipe.py`) and `ui/jobs.py` renders the Short again
  from clean_16x9.mp4 with another caption style or layout, only while the clip is READY (D7); the length
  cannot change after staging (the wider footage is not kept). Guards: the Host must be this machine, a POST
  needs the page's token and a same-host Origin, commands are fixed lists built from checked values, only
  the two videos of a clip folder are served. The app never approves, uploads or posts: that stays with the
  operators in Telegram. A run keeps every event it made (work/ui/runs/<id>/replay.jsonl), so the page can
  play a finished run back ("Watch it again"), runs of earlier sessions too. pytest started from the app
  runs with DRY_RUN=1 and without this machine's CLIPWATCHER_STAGING / CONFIG / LEDGER / EVENTS. The page
  lists the channels of config/streamers.yaml only (the example file is a template, not a list) and takes
  any other Twitch channel typed in; a Twitch VOD is held back with a sentence when TwitchDownloaderCLI is
  not installed. Streamers are shown with their profile picture, display name and badges (owner, 20 Nov):
  `ui/profiles.py` asks Twitch's GQL (picture, partner, the badges the streamer displays) and Kick's channel
  address (picture, verified, the channel's subscriber badges) without a login, keeps the answer for a day
  in work/ui/profiles/ and the pictures by the hash of their address, and the page loads them from the app
  only (https on static-cdn.jtvnw.net and files.kick.com, PNG/JPEG/GIF/WebP by their first bytes, 2 MB at
  most; a failed lookup keeps the last profile and is tried again after ten minutes). Kick has no picture
  for "verified": the page draws that tick. The caption library (D19): the Captions step and a clip's
  own settings share one picker; the chosen style plays on a stand-in Short, the shelf holds all 29
  (four built in, 25 animated) and Fine-tune opens every parameter of the template (text, phrases,
  timing, colours, motion, effects), each change played back. `ui/captions.py` reads what can be set
  and within what limits from the template's own schema in remotion/src (which stays the authority),
  refuses a value outside them or colours under the contrast floor, and makes each preview once
  (work/ui/captions/, named by what it shows; a padded render says when the words leave their room).
  "Use for new clips" writes `render.captions`, `render.rm.colors` and this machine's own version of
  the template (`render.rm.templates.<name>`) to config/local.yaml; a clip rendered again keeps its
  template, colour mode and settings in meta (caption_colors, caption_settings), and a template that
  cannot be rendered leaves the clip as it was. Without the Node setup the animated ones say what to
  run. Checked 20 Nov through the page on real streams: two live runs (kaicenat, clix) with a
  forced clip each, staged and played in the page; a Short rendered again with another caption style; a
  render stopped midway (the clip kept its video). Later the same day (owner: a Library to manage,
  configure and create caption templates, kept on this machine and pickable for any clip; the two keys set
  and tested in the page; YouTube accounts signed in through a Chrome window the page opens, listed with
  picture and numbers, the onboarding step by step and 3D; logs in plain words, not technical): six
  places, Run, Clips, Library, Insights, Connect, Settings. Library and Connect keep their requests in
  `ui/library.py` and `ui/connect.py` (`server.PLACES`) and their page in `static/library.*` and
  `static/connect.*`, built on `window.CW`. Library: a template of one's own is a shipped template under
  its own name with its own settings, `render.rm.presets.<id>` in config/local.yaml ({name, base,
  settings, created}); its caption style is `rm:<id>` wherever a shipped one goes (lanes, render again,
  previews); it stands on the base as it shipped, not on this machine's edit of the base; deleting one
  that is in use sends new clips back to its base; step 3 of Run only picks, fine-tuning is in the
  Library (a clip's own settings stay in its drawer). Connect: the Claude key and the Telegram bot token
  are kept in `.env` (mode 0600, every other line kept; the app loads the file at start and what the
  environment already sets wins) and never come back out of the server: the page gets "set" and the last
  four characters. Each has a check that costs nothing and sends nothing: the judge's model asked of the
  API; getMe, then getChat for the operators' group. YouTube accounts: one Chrome profile per account
  under ~/.clipwatcher/youtube (CLIPWATCHER_ACCOUNTS), signed in by `python -m
  clipwatcher.publish.youtube_login`, which the app starts one at a time and which only watches the
  address the window is on (it never types); uploads go through the profile of the account that posts
  (`YT_BROWSER_PROFILE` still wins; the single profile of before can be adopted). Plain words: every
  event goes to the page with a sentence (`ui/friendly.py`: what is happening, a known error as what it
  means and what to do), the steps have names a person would use, and the lane's own output is under
  "Technical details". Settings also holds the recording quality (720p, or the sharpest the stream
  offers: the webcam of a Short is cut out of the recording). Checked through the page: a template
  created, renamed, used to render a staged clip again, a shipped one edited and reset, all kept over a
  restart; made-up keys refused by the real services with a sentence; the sign-in with real Chrome up to
  the waiting step, cancel and every early end, no process left; a live run with the speech model and
  the face detector installed (a clip staged in 30 s, the story readable). Not run for real: a sign-in
  to its end (no account on the build machine: reading the channel and "ready to post" ran on a
  stand-in browser), a working key or token, a lane run with a template of one's own.
- D18 The webcam is read from the picture (owner, 19 Nov: verify adversarially, on many streams and frame
  by frame, that the webcam is found every time and the 9:16 looks right; measured 20 Nov, not there yet).
  `layout.resolve` finds the overlay's own rectangle in the frames around the moment and that rectangle is
  the cam crop, not a box guessed around the face (too tight: 27% more enlargement at the median, or
  reaching into the game); webcam-like rectangles are enlarged and searched again for small, dark or
  profile faces. The config counts only while the picture agrees: confirmed `layouts:` rects are checked
  against what is found (the rects of zackrawrr, clix, tfue and ishowspeed were elsewhere than the cam,
  and kaicenat, `has_webcam: false`, had one); `has_webcam: false` no longer skips detection, `layout:
  fixed` does. Panels are crops of the panel's own shape inside the picture, never stretched
  (`template.split_crops`, `fit_crop`). Checked with scripts/webcam_record.py, webcam_check.py and
  webcam_sharpness.py (record, label, detect, render, every frame, sharpness, gallery). On 31 channels the
  code had never seen: 119 of 155 moments laid out right (33% before), 20 channels right at all five
  moments, 23 of 31 Shorts right on every frame (54,899 frames; none with black bars, a stretched panel or
  a wrong length); on 59 development channels 260 of 295 moments (96 before). Still failing: bars and
  sponsor panels attached to the cam with no edge between them, cut-out cams away from a lower corner,
  dark cams on dark games, two people in a full-screen camera split as an overlay, a layout switched
  inside the clip. Sharpness: scaler and encoder lose little (PSNR 45 dB, SSIM 0.989 against a clean
  enlargement of the same crop); what was lost was the over-tight crop (median enlargement 4.25x -> 3.4x);
  unsharp and CAS were rejected (halos double on compressed input). A cam under about 300 px wide is
  limited by the source, and a 720p recording keeps 27-49% of 1080p's fine detail in the cam panel
  (`buffer.quality`; the owner's call: disk and line). Round two, the same day (the bar: a Short never
  shows something else as the webcam): `Layout` carries `sure`, `doubts` and `evidence`; what the picture
  cannot decide comes out as source `unsure` and is rendered as the whole 16:9 frame over its blurred copy
  (no panel, no reframe), the doubts in meta.layout_doubts and on the clip in the companion app. Guess paths
  are gone (a box around the judge's point, a cam-like rectangle with no face, a confirmed rect that is not
  in the picture, a face alone); the 9:16 reframe is for `fullcam` only, no longer for `no-face`, so a
  handheld camera with one face (IRL) now gets the whole frame unless the picture is still around the face
  or several faces are there (owner to say whether known IRL channels should be followed again: it needs a
  switch per streamer). Counted right / whole frame / wrong: 41 fresh channels 170 / 23 / 12 of 205 moments
  (168 / 0 / 37 before), Shorts 32 / 7 / 2; the 31 of round one 123 / 32 / 0 (120 / 0 / 35); the first 59
  channels 239 / 56 / 0 (261 / 0 / 34), so 46 moments that were right are now held back as well. Wrong on
  fresh: a reframe although an overlay cam is there (5), a panel where there is no cam (3: an empty
  inventory slot, a spectator in a broadcast), a sponsor panel or text strip inside the panel (2), a panel
  too tight inside a drawn frame (2). Round three, 20 Nov (a third fresh set, and the reframe back for
  known full-screen channels): a confirmed `has_webcam: false` scene in the streamer's `layouts:` with no
  gameplay crop now decides `fullcam` when there is a face, no webcam-sized rectangle of drawn sides
  around it and no cam proposal in the picture (D12's reframe is back for those channels; the picture
  still wins when it shows an overlay). In the rectangle: a thin label strip drawn over a side of the
  cam is peeled, and the room rule reads the rectangle's own sides. Not sure any more (whole frame): a
  full camera beside a rectangle that could be a webcam, a cut-out that is not at its place through the
  whole stretch, three sure sides with a face only the enlarged look found. Tried and taken out again
  because each held back far more right panels than wrong ones: "several faces, one placed like a
  cut-out" (11 right for 1 wrong) and "a line more than a third into the rectangle" (7 for 1). Counted
  right / whole frame / wrong, no confirmed scenes: a third fresh set of 47 channels 142 / 79 / 14 of 235
  moments (142 / 74 / 19 with round two's code), Shorts 30 / 12 / 5; fresh-2 169 / 32 / 4 (Shorts 30 / 9
  / 2); the 31 of round one 123 / 32 / 0; the first 59 channels 233 / 62 / 0; all four sets 667 / 205 /
  18 of 890 (round two 674 / 185 / 31). With the full-camera scene confirmed for the 29 channels that
  film full screen: 94 / 48 / 3 of their 145 moments (70 / 74 / 1 without). Wrong on the third set: a
  reframe although an overlay cam is there (8: a still title screen, menu or browser with one face on
  it), a panel where there is no cam (3: a video being watched, an emote), a cut-out on a full-camera
  broadcast (1), a frame or name bar inside the panel (2). What the picture cannot decide, and a
  confirmed scene or the judge can: one face in a still picture with no rectangle (a full camera, or a
  menu with a face on it: right 13 times, wrong 7), a face low in a moving picture (a cut-out webcam, or
  a mascot), a rectangle with a face that is a video being watched. Detection needs a face detector: mediapipe with
  models/blaze_face_short_range.tflite (README), else OpenCV; without one the configured default scene is
  used and the companion app says so on the step.
- D19 Animated caption templates (owner, 19-20 Nov: brand-new, highly styled, animated templates rendered
  with Remotion, tested frame by frame with no imperfection, an auto colour mode with high-attention
  colours and perfect contrast on the clip under them; and, said to the build directly: up to 25
  templates, every parameter set and open to settings, all totally different from each other, very smooth
  at the video's frame rate, brand-new animations tested like the templates, the only emoji Apple's).
  `render.captions: rm:<template>` picks one of 25 (the table is in remotion/README.md); every parameter
  of a template is in remotion/templates/<name>.json and `render.rm.templates.<name>` overrides any of
  them. The built-in styles stay the default (`word`) and the fallback (`render.rm.fallback`) whenever
  Node, the setup or a render is missing or fails: captions never cost a clip, and meta.captions is what
  went on the video (meta.captions_error says why after a fallback). How: Python plans phrases and timing
  (`render/rm_plan.py`) and picks colours (`render/palette.py`), Node renders transparent PNGs of the
  caption band only, at the clip's own frame rate (`render.rm.max_fps` 60), and ffmpeg blends the band
  in 4:4:4 (`render/remotion.py`). Text is the font's own outlines (browser text snaps to pixels, which
  shows as judder); nothing fades, text is always opaque and enters and leaves by moving; a library of 25
  named animations in five slots (enter, exit, active, emph, idle), of which `punch` is built, so each can
  be swapped by name. Colours: WCAG contrast, a floor of 4.5:1 on every frame and 7:1 for every pair a
  template ships with; `render.rm.colors: auto` samples the Short's own picture under each phrase and
  takes, role by role, the first candidate that keeps the floor, is not a hue the picture is already full
  of and is told apart from its neighbours; `fixed` keeps the template's own. Emoji: Apple Color Emoji
  where the system has it (macOS), never bundled (not redistributable); anywhere else emoji are taken out
  of the caption text. Checked by `render/framecheck.py` (tests marked `remotion`, skipped without the
  setup): every frame at 30 and 60 fps against the plan (what is shown and when, ink inside the caption
  box, no overlap, no jump, flash or single-frame element, the same motion at both rates, bundled fonts
  only, identical renders, the ffmpeg composite within 2 levels, the contrast floor over white, black,
  flashing, noise and stripes). Speed: about 13 s of caption rendering for 30 s of clip at 60 fps on eight
  idle cores, so too slow for the live lane's minute on a machine that is recording: built-in styles
  there, templates for VODs, or `max_fps: 30`. Setup once per machine: `cd remotion && npm ci && npm run
  setup` (pinned packages, a headless browser, the bundle); nothing is fetched at render time. Licence:
  Remotion is free for individuals and for companies of up to three people; a fourth person makes it a
  paid company licence (remotion/README.md has the links). Known limits: emoji move in whole pixels,
  script faces do not join (specks of video where two outlines almost meet were a third until the
  renderer closed them, below). Checked again after the build, 20 Nov: the seven fixtures on all 25
  templates at 30 and 60 fps, 50,850 frames and 4.59 M checks, no failure. Every frame of five staged
  Shorts under made-up fast talk (auto colours, 60 fps) then found one fault: arcade added a hop to a
  landing bounce (letters 2 px above the box for six frames; a letter now takes the higher of the two).
  Three reports were the checker's own faults, fixed: the speck count never left out what the outside
  reaches (Pillow's flood fill does nothing to an image made from an array, without a word; chips showed
  740 px on frames with none), a 2 px remnant of a letter under glide's box was read as its background,
  a unit scaled to nothing as a stutter. The talk of that clip is fixture `shouts` (full set): 22
  templates pass it at both rates; at 30 fps bounce (the landing flash), chips (a chip face) and comic
  (the punch) put a whole change into one frame when a word started just after a frame, and chrome,
  creep and glide left real specks (up to 180 px a frame). Closed the same day (branch captions-polish).
  The phase sweep (`framecheck --phases`: the talk moved by fractions of a frame; `--scene-only` lays
  the frames out and draws nothing) shows what a fixture's own word times hide: eight templates failed
  somewhere in the first sweep, five (neon, ransom, reel, slam, vogue) only at phases the plain run
  never rendered. Fixed: bounce (the flash comes up evenly), chips (a chip turns only to a face that
  will then be seen), comic (the punch takes 0.16 s), neon (a tube heats up evenly), ransom (scraps
  straighten and rattle in with the highlight), and in the plan no word is active for less than 0.1 s
  (`MIN_ACTIVE_S`, three frames at 30 fps: words with one timestamp are spaced that far apart, a word
  that starts in the last 0.1 s of a clip is not shown). Specks: the renderer's last step closes every
  see-through gap under 5 px that ink walls in (remotion/scripts/gaps.mjs; 13 templates had them) and
  `specks` now fails a frame. glide: to a word on another line the box goes straight across (stretched,
  it came down as a slab through the letters of both lines). Checked until the owner ended the runs
  (20 Nov, 16:00): the four phases of `shouts` with pixels at 30 fps on all 25, 16,775 frames and
  1.25 M checks, nothing left (two marker reports were the checker's: a word a wipe has passed draws
  nothing and cannot stutter); five real clips at 60 fps on the six changed templates, 59,760 frames,
  no failure; the full set with pixels at both rates on 16 templates (arcade to ransom), 40,544 frames,
  none; eight phases without pixels on the full set for 8 (arcade to glide), 163,520 frames, none. Not
  run on the merged code: the full set with pixels for reel, retro, slam, stamp, sticker, surf, sweep,
  typer and vogue (reel, slam and vogue under the 0.1 s rule only without pixels at 30 fps), the
  scene-only sweep from glitch on, real clips for the 19 templates not changed, the `remotion` tests as
  a whole (85 of 86 three commits before the end, the one failure the checker's speck count, fixed with
  a test; after the merge only a smoke run of four). Open: the first real-clip run (the code before all
  of this; 114 of 125 cases, 227,160 frames, typer and vogue never reached) also reported `frame_change`
  on marker (two frames), sweep (one) and punch (one frame whose pixels changed by 59 levels while the
  scene did not); marker's looks like the wipe case above, none of the three was run on real clips
  again. A gap right at 5 px can be closed in one frame and open in the next (15 px in 1,776 frames of
  `mix` at 60 fps, creep and jelly; no check looks for it). Render time with gap closing was measured
  only on a busy machine (no slower within the spread).
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
  (`captions=` a built-in style or `rm:<template>`, `report=` a dict filled with the style the Short got)
  and `render_clean(src, start_s, dur_s, out, *, profile="cpu_batch") -> Path`. `render/captions.py` ASS writer.
  CLI `python -m clipwatcher.render`.
- `render/remotion.py` (D19) `templates()` (the shipped ones), `presets()` and `base_name(name)` (this
  machine's own, D17), `load_template(name, overrides=None)`, `missing() -> str | None`,
  `available()`, `render_overlay(words, dur_s, template, out_dir, *, box, fps, ...) -> Overlay | None`,
  `overriding(values)` (render.rm for the renders inside the block), `RemotionError`. `render/rm_plan.py`
  `plan(words, dur_s, params, *, box_w, box_h, ...) -> Plan`; `render/palette.py` `contrast(a, b)`,
  `auto_colors(...)`, `PALETTE`; `render/framecheck.py` the frame-by-frame checks
  (`python -m clipwatcher.render.framecheck`).
- `judge/transcribe.py` `transcribe(media, start_s, dur_s, language) -> list[Word]` (faster-whisper optional).
  `judge/frames.py` `keyframes(media, start_s, dur_s, n=6, height=480) -> list[Path]`.
  `judge/claude.py` `screen(...)` (legacy), `judge_candidate(cand, words, chat_lines, frames, *, title, game,
  client=None) -> JudgeResult` (now runs v2) and `judge_clip(cand, words, chat_lines, [(t, jpg)], *, game,
  platform, language, task)`. `judge/compact.py` serializers + schema; `judge/frames.py keyframes_lowres`.
- `queue/db.py` ClipDB (SQLite: clips, jobs, audit; atomic claim, card tracking). `queue/staging.py`
  `staging_root()`, `stage_package(clip_id, short, clean, meta) -> Path`, `mark_approved(...)`.
  `queue/telegram_bot.py` `post_card(app, db, clip_id, video) -> bool`, `post_missing_cards`, `sweep`.
- `publish/youtube.py` behind DRY_RUN. `publish/youtube_browser.py` `profile_dir()` (the account that posts);
  `publish/youtube_accounts.py` `Accounts(root=None)`, `posting_profile()`; `publish/youtube_login.py` the
  sign-in process (`python -m clipwatcher.publish.youtube_login --account <id> [--check]`).
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
- `layout.py` `resolve(media, streamer) -> Layout`: per-clip scene detection. The webcam crop is the overlay's
  real border (`calibrate.overlay_rect`: persistent edges with moving pixels outside), never the chat under it;
  in game close-ups only a steady face counts as the cam (`calibrate.steady_faces`). Needs the `calibrate` extra
  (mediapipe) and models/blaze_face_short_range.tflite (gitignored: `cwr put` it to a new box). Streamers use different layouts and
  switch scenes mid-stream, so each clip matches its face position against confirmed `layouts:` in
  streamers.yaml; unknown scenes render with an auto rect and meta.layout="auto-new" for operator review.
- `events.py` `emit(kind, **data)`, `step(name, **data)` (no-op unless CLIPWATCHER_EVENTS names a file).
  `judge/ledger.py` `record(*, task, model, usage=None, streamer="", start_s=None, error="", **answer)`, `read()`.
  `render/recipe.py` `keep(folder, *, words, hook, layout, beats=None, actions=None, cold_open=None)`,
  `read(folder) -> dict | None` (keyword arguments for `render_short`, times from the clip's start).
- `ui/` companion app (D17), `python -m clipwatcher.ui`: `server.make_server(host, port, app=None) -> (server, App)`;
  `runs.command(kind, params) -> list[str]` (tests | live | vod | render), `runs.RunManager(root=None)`
  `.start(kind, params) -> Run`, `.stop(id)`, `.force(id, seconds)`, `.history()`; `data.clips() / clip(id) /
  media_path(id, name) / overview() / insights()`; `settings.schema()`, `settings.save(values, merge=False)`;
  `jobs.rerender(clip_id, captions=None, layout=None, root=None, *, colors=None, settings=None) -> Path`.
  `captions.library()`, `styles()`, `fields(style)`, `check(style, values) -> overrides`,
  `preview(style, values=None, root=None) -> {"file", "warnings"}`, `warm()`.
  `library.get/post(app, parts, ...)` and `connect.get/post(app, parts, ...)`, `connect.image(id)` (the
  requests of the two places, `server.PLACES`); `friendly.say(event)`, `problem(error)`, `explain(line)`,
  `dress(event)`; `keys.load_env()`, `save(which, value)`, `remove(which)`, `status()`,
  `test_claude(client=None)`, `test_telegram(transport=None)`; `settings.save_preset(...)`.
  `profiles.Profiles(root=None)` `.get(platform, login) -> dict`, `.many([(platform, login)])`,
  `.image(image_id) -> (Path, content type) | None`.
