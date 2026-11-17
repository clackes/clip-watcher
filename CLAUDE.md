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
.venv/bin/pytest -q            # -m "not ffmpeg" to skip render tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

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
- D5 One 9:16 template (1080x1920): bg | ClipVault top banner 1000x166 centred @y96 | webcam 1080x480 @y270 | hook pill
  y702 t<2.5s | gameplay 1080x540 @y750 | CTA banner 920x152 centred @y1300 | subs \pos(540,1495) 80px current word #FFE600
  110% (owner's call 17 Nov; banners from assets/branding/src via
  scripts/build_brand_assets.py) | dead zone 1536-1920. Credit {\an9\pos(1060,282)}. Background ambient blur or
  frame_gradient_<accent>.png (-loop 1); accent red/yellow/cyan from hash(clip_id). No webcam -> 16:9 frame
  1080x608 centred in y270-1290.
- D6 Judge (v2, 17 Nov): one static cached system prompt (prompts/judge_v2.md, no per-clip variables) + 8
  labelled 480x270 JPEG q65 frames + compact JSON {meta,tr,ch,task}; short-key output {ok,sc,t0,t1,hk,tt,ds,
  fl{sp,mu,gb},why}, caps/credit enforced in code (judge/compact.py). Thinking off, effort low (judge.* in
  global.yaml). judge.mode single | two_stage | off. Owner asked for spoiler/music/gambling flags; flagged
  clips are rejected (judge.reject_flags). Heuristic fallback on API errors. Cost: scripts/judge_cost_benchmark.py.
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
