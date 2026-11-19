"""Kick VOD lane: chat replay -> detect -> cut the windows from the VOD -> judge -> render -> stage.

Checked against the live service on 19 Nov 2026, all without a login:
- ``kick.com/api/v2/channels/<slug>/videos`` lists the channel's VODs with their start time, length and the
  address of the HLS playlist. (``/api/v1/video/<uuid>``, which this lane and yt-dlp used to ask, answers
  403.)
- the chat of a VOD is read with the same history endpoint as live chat (chat/kick.py); VODs 17 days old
  still had theirs.
- a window is cut by ffmpeg straight from the playlist, exact to the frame.
If the chat cannot be read, pass a saved chat log (work/chat/<slug>/<stream id>.jsonl) with ``--chat``.
Rendering and staging reuse ``vod.process_candidate``.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipwatcher import config, vod
from clipwatcher.chat import kick
from clipwatcher.chat.convert import read_jsonl, write_jsonl
from clipwatcher.chat.kick import _http_json, iso_to_ms
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


# --- Kick endpoints (isolated so they can be swapped) -------------------------------------------------
def fetch_vod_meta(slug: str, vid: str, get: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Start time, length, channel and playlist of a VOD, from the channel's public list of videos.
    ``vid`` is the uuid in the VOD's address (or the broadcast's numeric id). LookupError when the list does
    not have it: Kick lists the latest VODs only."""
    rows = (get or _http_json)(f"{kick.API}/channels/{slug.lower()}/videos")
    for v in rows if isinstance(rows, list) else rows.get("data") or []:
        video = v.get("video") or {}
        if vid.lower() in (str(video.get("uuid", "")).lower(), str(v.get("id")), str(video.get("id"))):
            start = v.get("start_time") or v.get("created_at")
            return {
                "start_ms": iso_to_ms(start) if start else None,
                "duration_s": (v.get("duration") or 0) / 1000 or None,
                "channel_id": v.get("channel_id"),
                "source": v.get("source"),
                "stream_id": str(v.get("id")),
                "category": ((v.get("categories") or [{}])[0] or {}).get("name"),
            }
    raise LookupError(f"{slug} has no listed VOD {vid}")


def download_chat(
    channel_id: int | str, start_ms: int, end_ms: int, *, fetch: Callable[..., Any] | None = None
) -> list[ChatMessage]:
    """The chat of a VOD; ts_ms counts from ``start_ms`` (ms into the VOD), as the detector expects."""
    msgs, _ = kick.replay(channel_id, start_ms, end_ms, fetch=fetch)
    for m in msgs:
        m.ts_ms = max(0, m.ts_ms - start_ms)
    return sorted(msgs, key=lambda m: m.ts_ms)


def pick_variant(master_url: str, master_text: str, quality: str = "1080p60") -> str:
    """Address of the rendition to cut from: the one named like ``quality`` (1080p60, 720p60), else the
    largest picture. The master playlist itself when it lists none."""
    best: tuple[int, str] | None = None
    lines = [ln.strip() for ln in master_text.splitlines()]
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        uri = next((x for x in lines[i + 1 :] if x and not x.startswith("#")), None)
        if not uri:
            continue
        url = urllib.parse.urljoin(master_url, uri)
        name = re.search(r'VIDEO="([^"]+)"', ln)
        if name and name.group(1).lower() == quality.lower():
            return url
        res = re.search(r"RESOLUTION=\d+x(\d+)", ln)
        height = int(res.group(1)) if res else 0
        if best is None or height > best[0]:
            best = (height, url)
    return best[1] if best else master_url


def build_cut_cmd(
    playlist_url: str, start_s: float, end_s: float, out: Path, ffmpeg: str | None = None
) -> list[str]:
    """ffmpeg: [start_s, end_s] of an HLS VOD into an mp4 that starts exactly at start_s. Re-encoded, so
    the first frame is the one asked for and not the keyframe before it (the lane's offsets rely on that)."""
    return [
        ffmpeg or config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, start_s):.3f}", "-i", playlist_url, "-t", f"{end_s - max(0.0, start_s):.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(out),
    ]  # fmt: skip


def _text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - the VOD's own https playlist
        return r.read().decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    vcfg = config.global_config().get("vod", {})
    ap = argparse.ArgumentParser(prog="clipwatcher.kickvod")
    ap.add_argument("--platform", default="kick")
    ap.add_argument("--vod", required=True, help="kick.com/<slug>/videos/<uuid> URL or bare uuid")
    ap.add_argument("--streamer", required=True, help="channel slug")
    ap.add_argument("--chat", type=Path, help="chat JSONL (ms from VOD start); skips the chat replay")
    ap.add_argument("--channel-id")
    ap.add_argument("--video", type=Path)
    ap.add_argument("--top", type=int, default=vcfg.get("top", 5))
    ap.add_argument("--before", type=float, default=vcfg.get("before_s", 20))
    ap.add_argument("--after", type=float, default=vcfg.get("after_s", 10))
    ap.add_argument("--gap", type=float, default=vcfg.get("gap_s", 90))
    ap.add_argument("--out", type=Path, help="staging folder (default: the shared one, CLIPWATCHER_STAGING)")
    ap.add_argument("--work", type=Path, default=Path("work"))
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--no-viewer-clips", action="store_true", help="skip the viewer-clip confirmation (D3)")
    ap.add_argument("--language", help="spoken language (default: streamers.yaml, then en)")
    ap.add_argument("--category", help="what was streamed, as Kick names it (default: what the VOD says)")
    a = ap.parse_args(argv)
    a.out = a.out or vod.staging_root()

    streamer = a.streamer.lower()
    vid = kick_vod_id(a.vod)
    work = a.work / vid
    work.mkdir(parents=True, exist_ok=True)
    language = (a.language or config.stream_language(streamer)).lower()
    if language != config.channel_language():
        msg = f"{streamer} speaks {language!r}; the channel is {config.channel_language()!r} only."
        print(msg, file=sys.stderr)
        return 2

    meta: dict[str, Any] = {}
    try:
        meta = fetch_vod_meta(streamer, vid)
    except Exception as e:  # noqa: BLE001 - network, a blocked request, a VOD no longer listed
        if not (a.chat and a.video):
            print(f"Kick VOD {vid} not found in {streamer}'s videos ({e}).", file=sys.stderr)
            return 2
    if a.chat:
        messages = read_jsonl(a.chat)
    elif (work / "chat.jsonl").exists():
        messages = read_jsonl(work / "chat.jsonl")  # read on an earlier run
    else:
        try:
            cid = a.channel_id or meta["channel_id"]
            if not (meta.get("start_ms") and cid):
                raise RuntimeError("VOD metadata incomplete")
            end_ms = meta["start_ms"] + int(((meta["duration_s"] or 0) or 14 * 3600) * 1000)
            hours = (meta["duration_s"] or 0) / 3600
            print(f"reading the chat of {hours:.1f} h (Kick allows about 35 messages a second) ...")
            messages = download_chat(cid, meta["start_ms"], end_ms)
        except Exception as e:  # noqa: BLE001
            print(
                f"Kick chat replay unavailable ({e}). Pass the chat log saved during the stream "
                f"(work/chat/{streamer}/<stream id>.jsonl) with --chat.",
                file=sys.stderr,
            )
            return 2
    write_jsonl(messages, work / "chat.jsonl")

    # D3: moments viewers clipped themselves are confirmed, as in the Twitch VOD lane
    clip_times: list[float] = []
    if meta.get("stream_id") and meta.get("start_ms") and not a.no_viewer_clips:
        from clipwatcher.detect.viewer_clips import kick_vod_clip_moments

        clip_times = kick_vod_clip_moments(streamer, meta["stream_id"], meta["start_ms"])
    cands = detect(
        messages, streamer=streamer, top=a.top * (2 if clip_times else 1), before_s=a.before, after_s=a.after,
        gap_s=a.gap, category=a.category or meta.get("category"),
    )  # fmt: skip
    cands = vod.confirmed_top(cands, clip_times, a.top)
    vod.print_table(cands)
    if not cands:
        print("no candidates")
        return 0

    playlist: list[str] = []

    def fetch_window(start_s: float, end_s: float, out: Path) -> None:
        if not playlist:
            src = meta["source"]
            playlist.append(pick_variant(src, _text(src), vcfg.get("quality", "1080p60")))
        vod.run_cmd(build_cut_cmd(playlist[0], start_s, end_s, out))

    failed = 0
    for n, c in enumerate(cands, 1):
        try:  # one clip that cannot be made (the VOD ends inside it, ffmpeg fails) is not the end of the run
            pkg = vod.process_candidate(
                n,
                c,
                streamer=streamer,
                vod=vod_url(streamer, vid),
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
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"failed #{n}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        print(f"staged #{n}: {pkg}" if pkg else f"rejected by the judge #{n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
