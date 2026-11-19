"""Test the spike detector against reality: replay a real chat through it, as the live lane would see it.

    python scripts/replay_chat.py --vod 2932469290 --login zackrawrr [--hours 6] [--join-at-h 3]
    python scripts/replay_chat.py --kick --vod <uuid of a Kick VOD> --login n3on [--hours 1]
    python scripts/replay_chat.py --jsonl work/chat/zackrawrr/318665167448.jsonl

--vod downloads the chat replay of a Twitch VOD (also one still being recorded) into work/replay/. With
--login the clips are compared with the channel's most viewed viewer-made clips of the last week that fall
in this VOD: a clip "hits" when the detector fired while that moment was on screen. --join-at-h starts the
detector that far into the stream, primed with the chat before it, the way a mid-stream start works.

Prints clips per hour, viewer clips hit, and how many of the batch detector's top 10 the live detector also
found (CLAUDE.md: keep >= 8/10).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from clipwatcher import config
from clipwatcher.chat import twitch_replay
from clipwatcher.chat.convert import write_jsonl
from clipwatcher.detect.detector import detect
from clipwatcher.detect.live import LiveDetector
from clipwatcher.models import ChatMessage

_VOD_Q = "query($id: ID!) { video(id: $id) { createdAt lengthSeconds owner { login } } }"
_CLIPS_Q = (
    "query($login: String!) { user(login: $login) { clips(first: 100, criteria: {period: LAST_WEEK,"
    " sort: VIEWS_DESC}) { edges { node { title viewCount videoOffsetSeconds durationSeconds video { id } } }"
    " } } }"
)


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def load_vod(vod: str, hours: float | None) -> tuple[list[ChatMessage], int, str]:
    """(chat, VOD start in epoch ms, owner login); the download is cached in work/replay/<vod>.jsonl."""
    v = twitch_replay._post({"query": _VOD_Q, "variables": {"id": vod}})["data"]["video"]
    end = min(v["lengthSeconds"], hours * 3600) if hours else v["lengthSeconds"]
    cache = config.ROOT / "work" / "replay" / f"{vod}-{int(end)}.jsonl"
    if not cache.exists():
        print(f"downloading chat replay of VOD {vod} (0-{end / 3600:.1f} h) ...")
        write_jsonl(twitch_replay.fetch(vod, 0, end), cache)
    return read(cache), _ms(v["createdAt"]), v["owner"]["login"]


def load_kick_vod(slug: str, vod: str, hours: float | None) -> tuple[list[ChatMessage], int, dict]:
    """(chat in epoch ms, VOD start, VOD facts) of a Kick VOD; the chat is cached in work/replay/. Kick lets
    history through at about 35 messages a second, so an hour of a busy chat takes several minutes."""
    from clipwatcher import kickvod
    from clipwatcher.chat import kick

    meta = kickvod.fetch_vod_meta(slug, kickvod.kick_vod_id(vod))
    dur = min(meta["duration_s"] or 0, hours * 3600) if hours else meta["duration_s"] or 0
    cache = config.ROOT / "work" / "replay" / f"kick-{meta['stream_id']}-{int(dur)}.jsonl"
    if not cache.exists():
        print(f"reading the chat of Kick VOD {vod} (0-{dur / 3600:.1f} h) ...")
        msgs, _ = kick.replay(meta["channel_id"], meta["start_ms"], meta["start_ms"] + dur * 1000)
        write_jsonl(msgs, cache)
    return read(cache), int(meta["start_ms"]), meta


def kick_clips(slug: str, stream_id: str, t0_ms: int) -> list[dict]:
    """The channel's most viewed clips of the week that belong to this broadcast, on the VOD's timeline."""
    from clipwatcher.chat import kick
    from clipwatcher.detect.viewer_clips import KICK_CLIPS, ViewerClip, parse_rfc3339

    d = kick._http_json(KICK_CLIPS.format(slug=slug) + "?cursor=0&sort=view&time=week", {})
    out = []
    for c in d.get("clips") or []:
        if str(c.get("livestream_id")) == str(stream_id) and c.get("duration"):
            a, b = ViewerClip(str(c["id"]), parse_rfc3339(c["created_at"]), "", float(c["duration"])).window
            out.append({"title": c.get("title") or "", "viewCount": c.get("views") or 0,
                        "videoOffsetSeconds": a - t0_ms / 1000, "durationSeconds": b - a})  # fmt: skip
    return out


def read(path: Path) -> list[ChatMessage]:
    out, fields = [], ChatMessage.__dataclass_fields__
    for line in path.read_text(encoding="utf-8").splitlines():
        try:  # fields this version does not know (a log written by another one) are left out
            out.append(ChatMessage(**{k: v for k, v in json.loads(line).items() if k in fields}))
        except (ValueError, TypeError):
            continue
    return sorted(out, key=lambda m: m.ts_ms)


def viewer_clips(login: str, vod: str) -> list[dict]:
    d = twitch_replay._post({"query": _CLIPS_Q, "variables": {"login": login}})
    nodes = [
        e["node"] for e in (((d.get("data") or {}).get("user") or {}).get("clips") or {}).get("edges", [])
    ]
    return [n for n in nodes if (n.get("video") or {}).get("id") == vod and n.get("videoOffsetSeconds")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--vod", help="Twitch VOD id")
    src.add_argument("--jsonl", type=Path, help="saved chat log (work/chat/<login>/<stream id>.jsonl)")
    ap.add_argument("--login", help="channel login: compare with its viewer-made clips (needs --vod)")
    ap.add_argument("--kick", action="store_true", help="--vod is the uuid of a Kick VOD of channel --login")
    ap.add_argument("--hours", type=float, help="only the first N hours of the VOD")
    ap.add_argument("--category", help='the kind of stream ("Grand Theft Auto VI"): its keywords apply (D16)')
    ap.add_argument(
        "--join-at-h", type=float, default=0.0, help="the bot joins this far in, primed with history"
    )
    a = ap.parse_args()

    kick_meta = None
    if a.kick:
        if not (a.vod and a.login):
            ap.error("--kick needs --vod <uuid> and --login <slug>")
        msgs, t0_ms, kick_meta = load_kick_vod(a.login.lower(), a.vod, a.hours)
        owner = a.login.lower()
    elif a.vod:
        msgs, t0_ms, owner = load_vod(a.vod, a.hours)
    else:
        msgs, owner = read(a.jsonl), a.jsonl.parent.name
        t0_ms = msgs[0].ts_ms
    streamer = a.login or owner
    t0 = t0_ms / 1000
    cut_ms = t0_ms + a.join_at_h * 3.6e6
    det = LiveDetector(streamer, category=a.category)
    primed = det.prime(m for m in msgs if m.ts_ms < cut_ms) if a.join_at_h else 0
    live = [c for m in msgs if m.ts_ms >= cut_ms and (c := det.feed(m))] + det.drain()
    hours = (msgs[-1].ts_ms - max(cut_ms, msgs[0].ts_ms)) / 3.6e6
    print(
        f"{streamer}: {len(msgs)} messages, {hours:.1f} h watched"
        + (f", primed with {primed}" if primed else "")
    )
    print(f"live detector: {len(live)} clips = {len(live) / max(hours, 1e-9):.1f} per hour")
    for c in sorted(live, key=lambda c: -c.signals.get("session_pct", 0))[:10]:
        s = c.signals
        print(
            f"  {(c.peak_s - t0) / 3600:5.2f} h  score {c.chat_score:5.1f}  gate {s['gate']:4.1f}  "
            f"{c.unique_chatters:3d} chatters  x{c.x_normal:.1f} normal  "
            f"above {100 * s.get('session_pct', 0):.2f}% of the stream, rank {s.get('session_rank', 0):.0f}"
        )
    later = [m for m in msgs if m.ts_ms >= cut_ms]
    batch = detect(later, streamer=streamer, top=10, gap_s=90, category=a.category)
    same = sum(1 for b in batch if any(abs(c.peak_s - b.peak_s) <= 30 for c in live))
    print(f"batch top {len(batch)}: live found {same} of them")
    if a.vod and a.login:
        every = (
            kick_clips(a.login.lower(), kick_meta["stream_id"], t0_ms)
            if kick_meta
            else viewer_clips(a.login, a.vod)
        )
        clips = [
            c for c in every
            if a.join_at_h * 3600 + 120 < c["videoOffsetSeconds"] < (msgs[-1].ts_ms - t0_ms) / 1000 - 30
        ]  # fmt: skip
        fires = [c.signals["fire_s"] - t0 for c in live]

        def on_screen(c: dict) -> bool:
            lo, hi = c["videoOffsetSeconds"] - 5, c["videoOffsetSeconds"] + c["durationSeconds"] + 20
            return any(lo <= f <= hi for f in fires)

        print(
            f"viewer-made clips in this stretch: {len(clips)}, detector fired on {sum(map(on_screen, clips))}"
        )
        for c in sorted(clips, key=lambda c: -c["viewCount"])[:8]:
            mark = "hit " if on_screen(c) else "miss"
            at = c["videoOffsetSeconds"] / 3600
            print(f"  {mark} {at:5.2f} h  {c['viewCount']:6d} views  {c['title'][:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
