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
Code is edited and committed on the Mac (`main`); the bot runs only on the Windows Server 2025 box
(`C:\clip-watcher`, since 18 Nov), driven from the Mac over WinRM. No GitHub and no SSH (owner's choice).
The Mac has no venv (space): tests run on the server.
```
cwr() { uv run -q --with pywinrm==0.5.0 scripts/remote/cw_remote.py "$@"; }   # password: Keychain "clipwatcher-winrm"
cwr push            # commit first; sends the server's missing commits as a git bundle
cwr test            # pytest on the server (default -m "not ffmpeg"; pass args for more)
cwr restart [xqc,lacy|all]   # restart the bot (new code); a list is saved in config\live_streamers.txt
cwr status ; cwr logs -n 80 -g 'judge|ERROR' ; cwr stop ; cwr put/get <file> ; cwr ps script.ps1
uvx ruff check . && uvx ruff format --check .
```
- On the server: scheduled task `cw-live` (interactive, window "Clip Watcher LIVE") runs
  `scripts\run_live_windows.ps1` (loads .env). Only one Telegram bot per token may poll: never start a second copy.
- `config/streamers.yaml` / `operators.yaml` are not in git: edit on the Mac, `cwr put` them to `C:\clip-watcher\config\`.
- Remote desktop (YouTube login, watching windows): `scripts/remote/clipwatcher-server.rdp` in Windows App.
  YouTube login: `run_live_windows.ps1 -YtLogin -Account <username>` on the server desktop.
- Logs: `work\logs\live-<yyyymmdd>.log` (UTF-8). ffmpeg 8.1 (libass) in `C:\ffmpeg\bin` via CLIPWATCHER_FFMPEG.
- YouTube uploads use the patchright Chrome profile `~\.clipwatcher\yt-profiles\<YT_BROWSER_ACCOUNT>` (capybarasking).
- Windows needs the VC++ runtime for Whisper (installed). Mac/Linux: `.venv/bin/...` equivalents still work.

## Testing against reality
- Replay real VOD chats through `LiveDetector` and compare with batch `detect()` top-10 (see git log
  81afd37). Keep >= 8/10 matches on clix/zackrawrr/tfue/kaicenat VODs when changing the detector.
- `scripts/force_clip.py` pushes one forced moment through the live pipeline on a running ring buffer.

## Decision log (v2.1, binding)
- D1 Ingestion: streamlink -> .ts + anonymous Twitch IRC TLS (irc.chat.twitch.tv:6697) + Kick Pusher/webhooks.
  patchright only as optional Kick Cloudflare cookie adapter, never video capture.
- D2 Ring buffer: 10 s .ts segments, 15-60 min window (default 30). Candidates pin window +/-60 s. Pins expire
  after render or 48 h. Pruning stops and alerts below 15% free disk.
- D3 Spike trigger: chat_score = 0.5*z(U) + 0.35*z(E) + 0.15*z(S), median/MAD over 15 min. E uses
  config/global.yaml keywords merged with per-streamer extras. Bot/spam/giveaway/raid filters. Confirm with
  viewer clips + audio loudness (final >= 4.5).
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
  (render/jumpcut.py: 0.8-4 s pauses, never below 12 s / 60 %, beats and action labels protected); judge `ac`
  action labels (*WATCHING*) on silent stretches; labels and sounds only in pauses (template.cues); no-webcam
  full-camera / IRL clips reframed to a full-screen 9:16 window that follows the face (render/reframe.py).
  Judge: thinking on at low effort (off, the same clip scored 5-8), a frame every 2.5 s (8-16), audio peaks `au`.
- D6 Judge (v2, 17 Nov): one static cached system prompt (prompts/judge_v2.md, no per-clip variables) + 8
  labelled 480x270 JPEG q65 frames + compact JSON {meta,tr,ch,task}; short-key output {ok,sc,t0,t1,hk,tt,ds,
  fl{sp,mu,gb},why}, caps/credit enforced in code (judge/compact.py). Thinking off, effort low (judge.* in
  global.yaml). judge.mode single | two_stage | off. Owner asked for spoiler/music/gambling flags; sp
  clips are rejected (judge.reject_flags); gb (17 Nov) and mu (18 Nov, owner accepts music-claim risk) are informational. Heuristic fallback on API errors (English fallback copy, same shape). Judge t0/t1 start late (<= 2 s before the
  action; fallback 10 s before the reaction), never cut a word, end on a finished sentence (judge/snap.py).
- D10 English only (owner, 17 Nov): `channel.language: en`. All copy (hook, title, description, fallbacks) is
  English; streamers whose `language` differs are skipped by live/vod. Description = 4 lines: teaser + credit,
  a clip-specific question ending in 👇, a reason to follow (no "like/comment/follow" begging, stripped in
  code by `compact.is_bait`), hashtags. Cost: scripts/judge_cost_benchmark.py.
- D7 Telegram queue with atomic SQLite claim lock (15 min TTL), audit log, staging/<clip_id>/, staging/approved/,
  staging/published_clips.json.
- D8 Publishing: YouTube Data API + Instagram Graph API from server, APPROVED only. TikTok/campaign forms by a
  human with Claude in Chrome from staging/approved.
- D9 .ts seek bug: never `-ss` exact on MPEG-TS. Coarse input seek to max(0, START-6), then
  trim/atrim=start=TRIM in the graph, then setpts=PTS-STARTPTS. Regression test required.

## Module map and interfaces (keep signatures stable)
- `models.py` ChatMessage, Candidate, JudgeResult, Word. `config.py` loaders + tool paths.
- `chat/convert.py` `tdl_to_messages(path) -> list[ChatMessage]`, `read_jsonl/write_jsonl`.
- `detect/detector.py` `detect(messages, *, streamer, viewers=None, top, before_s, after_s, gap_s) -> list[Candidate]`.
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
- `queue/db.py` ClipDB (SQLite: clips, jobs, audit; atomic claim). `queue/staging.py`
  `stage_package(clip_id, short, clean, meta) -> Path`, `mark_approved(...)`. `queue/telegram_bot.py`.
- `publish/youtube.py` behind DRY_RUN.
- `vod.py` Day-0 VOD lane CLI `python -m clipwatcher.vod`.
- Live ingest (`ingest/`): `ringbuffer.RingBuffer(root, window_min, segment_s)` with `add_segment(path, pdt_ms)`,
  `pin(start_ms, end_ms, ttl_s)`, `prune()`, `cut(start_ms, end_ms, out) -> Path` (wall-clock epoch ms via
  #EXT-X-PROGRAM-DATE-TIME map). `watcher.py` live polling (Helix / Kick), `recorder.py` streamlink supervisor.
- Live chat (`chat/twitch_irc.py`, `chat/kick.py`): async iterators yielding ChatMessage with epoch-ms ts.
  `detect/live.py` `LiveDetector(streamer, viewers=None).feed(msg) -> Candidate | None` (epoch seconds).
  `detect/viewer_clips.py` Helix Get Clips poller.
- `fastlane.py` live lane orchestrator; `calibrate.py` facecam/gameplay rect suggestion + Telegram still.
- `layout.py` `resolve(media, streamer) -> Layout`: per-clip scene detection. Streamers use different layouts and
  switch scenes mid-stream, so each clip matches its face position against confirmed `layouts:` in
  streamers.yaml; unknown scenes render with an auto rect and meta.layout="auto-new" for operator review.
