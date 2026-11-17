"""Day-0 VOD lane: chat -> detector -> download candidate windows -> judge -> render -> stage.

DRY_RUN is the default; this module never posts anywhere, it only stages packages.
"""

from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from clipwatcher import config
from clipwatcher.chat.convert import tdl_to_messages, write_jsonl
from clipwatcher.detect.detector import detect
from clipwatcher.models import Candidate, ChatMessage


def vod_id(vod: str) -> str:
    m = re.search(r"(\d{5,})", vod)
    return m.group(1) if m else re.sub(r"\W+", "_", vod)


def build_chatdownload_cmd(tdl: str, vod: str, out: Path) -> list[str]:
    return [tdl, "chatdownload", "--id", vod_id(vod), "-o", str(out)]


def build_videodownload_cmd(
    tdl: str, vod: str, start_s: float, end_s: float, out: Path, quality: str = "1080p60"
) -> list[str]:
    # Verify flags with `TwitchDownloaderCLI videodownload --help` after upgrading the tool
    # (-b / -e take a unit suffix: 12s, 3m, 1h; -q is the quality label).
    return [
        tdl,
        "videodownload",
        "--id",
        vod_id(vod),
        "-b",
        f"{int(max(0, start_s))}s",
        "-e",
        f"{int(end_s)}s",
        "-q",
        quality,
        "-o",
        str(out),
    ]


def run_cmd(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def window_with_pad(cand: Candidate, pad_s: float) -> tuple[float, float, float]:
    """Return (download_start, download_end, local_start_offset_of_candidate)."""
    dl_start = max(0.0, cand.start_s - pad_s)
    return dl_start, cand.end_s + pad_s, cand.start_s - dl_start


def heuristic_meta(streamer: str) -> dict:
    return {
        "hook": "CHAT LOST IT",
        "titles": [f"{streamer} chat loses it", f"{streamer}: this moment broke chat"],
        "setup_line": "",
        "judge_source": "none",
    }


def _mod(name: str):
    return importlib.import_module(name)


def _chat_lines(messages: list[ChatMessage], start_s: float, end_s: float, limit: int = 60) -> list[str]:
    sel = [m for m in messages if start_s * 1000 <= m.ts_ms <= end_s * 1000 and not m.is_notice]
    return [f"{m.user}: {m.text}" for m in sel[:limit]]


def print_table(cands: list[Candidate]) -> None:
    print(f"{'#':>2} {'time':>9} {'chat':>7} {'xnorm':>6} {'U':>4}")
    for i, c in enumerate(cands, 1):
        h, rem = divmod(int(c.peak_s), 3600)
        m, s = divmod(rem, 60)
        print(f"{i:>2} {h:d}:{m:02d}:{s:02d} {c.chat_score:7.2f} {c.x_normal:6.1f} {c.unique_chatters:4d}")


def process_candidate(
    n: int,
    cand: Candidate,
    *,
    streamer: str,
    vod: str,
    vid: str,
    work: Path,
    out_dir: Path,
    video: Path | None,
    messages: list[ChatMessage],
    language: str,
    judge: bool,
    pad_s: float,
    quality: str,
    fetch_window: Callable[[float, float, Path], None] | None = None,
    platform: str = "twitch",
) -> Path:
    scfg = config.streamer(streamer)
    clip_id = f"{streamer}-{vid}-{int(cand.start_s * 1000)}"
    dur = cand.end_s - cand.start_s
    if video:
        src, local_start = video, cand.start_s
    else:
        dl_start, dl_end, local_start = window_with_pad(cand, pad_s)
        src = work / f"cand_{n}_{int(dl_start)}.mp4"
        if not (src.exists() and src.stat().st_size > 0):  # reuse an earlier download of the same window
            if fetch_window:
                fetch_window(dl_start, dl_end, src)
            else:
                run_cmd(
                    build_videodownload_cmd(
                        config.twitch_downloader_bin(), vod, dl_start, dl_end, src, quality
                    )
                )

    meta = heuristic_meta(streamer)
    words: list = []
    if judge:
        transcribe = _mod("clipwatcher.judge.transcribe")
        frames_m = _mod("clipwatcher.judge.frames")
        claude = _mod("clipwatcher.judge.claude")
        words = transcribe.transcribe(src, local_start, dur, language)
        frames = frames_m.keyframes(src, local_start, dur)
        jr = claude.judge_candidate(
            cand,
            words,
            _chat_lines(messages, cand.start_s, cand.end_s),
            frames,
            title=f"{streamer} VOD {vid}",
            game=scfg.get("game"),
        )
        meta.update(
            hook=jr.hook_text,
            titles=jr.title_options,
            setup_line=jr.setup_line,
            judge_source=jr.source,
            funny_score=jr.funny_score,
            hype_score=jr.hype_score,
            reject_reason=jr.reject_reason,
        )
        if 0 <= jr.trim_start_s < jr.trim_end_s <= dur + 0.5:
            local_start, dur = local_start + jr.trim_start_s, jr.trim_end_s - jr.trim_start_s

    tpl = _mod("clipwatcher.render.template")
    clip_dir = work / clip_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    short = tpl.render_short(
        src,
        local_start,
        dur,
        clip_dir / "short_9x16.mp4",
        clip_id=clip_id,
        streamer=streamer,
        words=words,
        hook=meta["hook"],
        has_webcam=bool(scfg.get("has_webcam", True)),
        cam=scfg.get("facecam_rect"),
        game=scfg.get("gameplay_rect"),
    )
    clean = tpl.render_clean(src, local_start, dur, clip_dir / "clean_16x9.mp4")
    meta.update(
        clip_id=clip_id,
        streamer=streamer,
        vod_id=vid,
        credit=f"{platform}.{'tv' if platform == 'twitch' else 'com'}/{streamer}",
        campaign_link=scfg.get("campaign_link"),
        chat_score=cand.chat_score,
        final_score=cand.final_score,
        x_normal=cand.x_normal,
        unique_chatters=cand.unique_chatters,
        peak_s=cand.peak_s,
        start_s=cand.start_s,
        end_s=cand.end_s,
        state="READY",
        out_dir=str(out_dir),
    )
    staging = _mod("clipwatcher.queue.staging")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    db = _mod("clipwatcher.queue.db").ClipDB(Path(out_dir) / "clips.db")
    return staging.stage_package(clip_id, short, clean, meta, root=out_dir, db=db)


def main(argv: list[str] | None = None) -> int:
    vcfg = config.global_config().get("vod", {})
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--platform", choices=["twitch", "kick"], default="twitch")
    if pre.parse_known_args(argv)[0].platform == "kick":
        return _mod("clipwatcher.kickvod").main(argv)
    ap = argparse.ArgumentParser(prog="clipwatcher.vod")
    ap.add_argument("--platform", choices=["twitch", "kick"], default="twitch")
    ap.add_argument("--vod", required=True)
    ap.add_argument("--streamer", required=True)
    ap.add_argument("--chat", type=Path)
    ap.add_argument("--video", type=Path)
    ap.add_argument("--top", type=int, default=vcfg.get("top", 5))
    ap.add_argument("--before", type=float, default=vcfg.get("before_s", 20))
    ap.add_argument("--after", type=float, default=vcfg.get("after_s", 10))
    ap.add_argument("--gap", type=float, default=vcfg.get("gap_s", 90))
    ap.add_argument("--out", type=Path, default=Path("staging"))
    ap.add_argument("--work", type=Path, default=Path("work"))
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--language", choices=["it", "en"])
    a = ap.parse_args(argv)

    streamer = a.streamer.lower()
    vid = vod_id(a.vod)
    work = a.work / vid
    work.mkdir(parents=True, exist_ok=True)
    language = a.language or config.streamer(streamer).get("language", "it")

    chat_path = a.chat
    if chat_path is None:
        chat_path = work / "chat.json"
        run_cmd(build_chatdownload_cmd(config.twitch_downloader_bin(), a.vod, chat_path))
    messages = tdl_to_messages(chat_path)
    write_jsonl(messages, work / "chat.jsonl")

    cands = detect(messages, streamer=streamer, top=a.top, before_s=a.before, after_s=a.after, gap_s=a.gap)
    print_table(cands)
    if not cands:
        print("no candidates")
        return 0
    for n, c in enumerate(cands, 1):
        pkg = process_candidate(
            n,
            c,
            streamer=streamer,
            vod=a.vod,
            vid=vid,
            work=work,
            out_dir=a.out,
            video=a.video,
            messages=messages,
            language=language,
            judge=not a.no_judge,
            pad_s=float(vcfg.get("pad_s", 6)),
            quality=vcfg.get("quality", "1080p60"),
        )
        print(f"staged #{n}: {pkg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
