"""Kick VOD lane: yt-dlp window download + unofficial chat replay -> detect -> render -> stage.

Everything that talks to Kick is UNVERIFIED (unofficial endpoints, may sit behind Cloudflare). If the
chat replay endpoint is unreachable, run the live chat logger (chat/kick.py + chat/live_log.py)
during the stream and pass the resulting JSONL with ``--chat``.
Rendering and staging reuse ``vod.process_candidate``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clipwatcher import config, vod
from clipwatcher.chat.convert import read_jsonl, write_jsonl
from clipwatcher.chat.kick import _http_json, api_message_to_chat, iso_to_ms
from clipwatcher.detect.detector import detect
from clipwatcher.models import ChatMessage

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def kick_vod_id(vod_url: str) -> str:
    m = _UUID.search(vod_url)
    return m.group(0).lower() if m else re.sub(r"\W+", "_", vod_url)


def vod_url(slug: str, vid: str) -> str:
    return vid if vid.startswith("http") else f"https://kick.com/{slug}/videos/{vid}"


def hms(sec: float) -> str:
    s = int(max(0, sec))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def build_ytdlp_cmd(url: str, start_s: float, end_s: float, out: Path, ytdlp: str = "yt-dlp") -> list[str]:
    return [
        ytdlp,
        "--download-sections",
        f"*{hms(start_s)}-{hms(end_s + 0.999)}",
        "--force-keyframes-at-cuts",
        "--no-playlist",
        "--merge-output-format",
        "mp4",
        "-o",
        str(out),
        url,
    ]


# --- unofficial Kick endpoints (isolated so they can be swapped) -------------------------------------
def fetch_vod_meta(vid: str, get: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """UNVERIFIED: https://kick.com/api/v1/video/<uuid> -> start time and channel id."""
    d = (get or _http_json)(f"https://kick.com/api/v1/video/{vid}")
    ls = d.get("livestream") or {}
    start = ls.get("start_time") or ls.get("created_at") or d.get("created_at")
    return {
        "start_ms": iso_to_ms(
            start.replace(" ", "T") + ("Z" if "+" not in start and "Z" not in start else "")
        )
        if start
        else None,
        "duration_s": (ls.get("duration") or 0) / 1000 or None,
        "channel_id": ls.get("channel_id"),
    }


def fetch_chat_page(
    channel_id: int | str, start_iso: str, get: Callable[[str], Any] | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """UNVERIFIED chat replay: https://kick.com/api/v2/channels/{id}/messages?start_time=<iso>.
    Returns (raw message dicts, start_time for the next page or None)."""
    d = (get or _http_json)(f"https://kick.com/api/v2/channels/{channel_id}/messages?start_time={start_iso}")
    data = d.get("data") or {}
    return data.get("messages") or [], data.get("cursor")


def download_chat(
    channel_id: int | str,
    start_ms: int,
    end_ms: int,
    *,
    page: Callable[[str], tuple[list[dict[str, Any]], str | None]] | None = None,
    max_pages: int = 20000,
) -> list[ChatMessage]:
    """Page the replay endpoint from the stream start until ``end_ms``; ts_ms is relative to ``start_ms``."""
    bots = {b.lower() for b in config.global_config().get("detector", {}).get("bots", [])}
    fetch = page or (lambda iso: fetch_chat_page(channel_id, iso))
    cur = datetime.fromtimestamp(start_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    out: dict[str, ChatMessage] = {}
    for _ in range(max_pages):
        raw, nxt = fetch(cur)
        if not raw:
            break
        newest = 0
        for d in raw:
            m = api_message_to_chat(d, bots)
            if m:
                newest = max(newest, m.ts_ms)
                out[str(d.get("id", f"{m.ts_ms}:{m.user}:{m.text}"))] = m
        if nxt:
            cur = nxt
        elif newest:
            cur = (
                datetime.fromtimestamp(newest / 1000 + 0.001, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            )
        else:
            break
        if newest >= end_ms:
            break
    msgs = [m for m in out.values() if start_ms <= m.ts_ms <= end_ms]
    for m in msgs:
        m.ts_ms -= start_ms
    return sorted(msgs, key=lambda m: m.ts_ms)


def main(argv: list[str] | None = None) -> int:
    vcfg = config.global_config().get("vod", {})
    ap = argparse.ArgumentParser(prog="clipwatcher.kickvod")
    ap.add_argument("--platform", default="kick")
    ap.add_argument("--vod", required=True, help="kick.com/<slug>/videos/<uuid> URL or bare uuid")
    ap.add_argument("--streamer", required=True, help="channel slug")
    ap.add_argument("--chat", type=Path, help="chat JSONL (ms from VOD start); skips the replay endpoint")
    ap.add_argument("--channel-id")
    ap.add_argument("--video", type=Path)
    ap.add_argument("--top", type=int, default=vcfg.get("top", 5))
    ap.add_argument("--before", type=float, default=vcfg.get("before_s", 20))
    ap.add_argument("--after", type=float, default=vcfg.get("after_s", 10))
    ap.add_argument("--gap", type=float, default=vcfg.get("gap_s", 90))
    ap.add_argument("--out", type=Path, default=Path("staging"))
    ap.add_argument("--work", type=Path, default=Path("work"))
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--language", help="spoken language (default: streamers.yaml, then en)")
    a = ap.parse_args(argv)

    streamer = a.streamer.lower()
    vid = kick_vod_id(a.vod)
    url = vod_url(streamer, a.vod if a.vod.startswith("http") else vid)
    work = a.work / vid
    work.mkdir(parents=True, exist_ok=True)
    language = (a.language or config.stream_language(streamer)).lower()
    if language != config.channel_language():
        msg = f"{streamer} speaks {language!r}; the channel is {config.channel_language()!r} only."
        print(msg, file=sys.stderr)
        return 2

    if a.chat:
        messages = read_jsonl(a.chat)
    else:
        try:
            meta = fetch_vod_meta(vid)
            cid = a.channel_id or meta["channel_id"]
            if not (meta["start_ms"] and cid):
                raise RuntimeError("VOD metadata incomplete")
            end_ms = meta["start_ms"] + int(((meta["duration_s"] or 0) or 14 * 3600) * 1000)
            messages = download_chat(cid, meta["start_ms"], end_ms)
        except Exception as e:  # noqa: BLE001 - network / Cloudflare / schema drift
            print(
                f"Kick chat replay unavailable ({e}). The Kick VOD lane needs live chat logging: run the "
                "live logger during the stream and pass the JSONL with --chat.",
                file=sys.stderr,
            )
            return 2
    write_jsonl(messages, work / "chat.jsonl")

    cands = detect(messages, streamer=streamer, top=a.top, before_s=a.before, after_s=a.after, gap_s=a.gap)
    vod.print_table(cands)
    if not cands:
        print("no candidates")
        return 0

    def fetch_window(start_s: float, end_s: float, out: Path) -> None:
        vod.run_cmd(build_ytdlp_cmd(url, start_s, end_s, out))

    for n, c in enumerate(cands, 1):
        pkg = vod.process_candidate(
            n,
            c,
            streamer=streamer,
            vod=url,
            vid=vid,
            work=work,
            out_dir=a.out,
            video=a.video,
            messages=messages,
            language=language,
            judge=not a.no_judge,
            pad_s=float(vcfg.get("pad_s", 6)),
            quality=vcfg.get("quality", "1080p60"),
            fetch_window=fetch_window,
            platform="kick",
        )
        print(f"staged #{n}: {pkg}" if pkg else f"rejected by the judge #{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
