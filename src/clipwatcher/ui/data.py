"""What the companion app shows, read from where the bot already keeps it: the staging folder and its queue
database (clips), the judge's ledger (cost, decisions), each clip's meta (timings, score make-up) and the
configuration. Nothing here writes."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from clipwatcher import config
from clipwatcher.judge import ledger
from clipwatcher.queue.staging import staging_root
from clipwatcher.render.captions import CAPTION_STYLES

CLIP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}")
FILES = ("short_9x16.mp4", "clean_16x9.mp4")
STEP_ORDER = (
    "wait",
    "chat",
    "cut",
    "loudness",
    "transcribe",
    "screen",
    "judge",
    "layout",
    "render_short",
    "render_clean",
    "stage",
)
STEP_NAMES = {
    "wait": "Wait for the recording",
    "chat": "Read the chat",
    "cut": "Cut the window",
    "loudness": "Measure loudness",
    "transcribe": "Transcribe",
    "screen": "Screen the text",
    "judge": "Judge",
    "layout": "Find the webcam",
    "render_short": "Render the 9:16 Short",
    "render_clean": "Render the clean 16:9",
    "stage": "Stage for approval",
}
CAPTIONS = {
    "word": ("Word", "One word at a time on the seam. Shouted words turn yellow."),
    "pop": ("Pop", "Three words a line. The spoken word turns yellow and pops."),
    "classic": ("Classic", "A full line in the lower third, the current word highlighted."),
    "bounce": ("Bounce", "Words bounce in, each in its own colour."),
}


def _platform(meta: dict[str, Any]) -> str:
    credit = str(meta.get("credit") or "")
    if credit.startswith("kick.com"):
        return "kick"
    if credit.startswith("twitch.tv"):
        return "twitch"
    return str(config.streamer(str(meta.get("streamer") or "")).get("platform") or "twitch")


def _states(root: Path) -> dict[str, dict[str, Any]]:
    """Queue rows by clip id, when the staging folder has a queue database (never created from here)."""
    db_path = root / "clips.db"
    if not db_path.exists():
        return {}
    try:
        from clipwatcher.queue.db import ClipDB

        return {r["id"]: r for r in ClipDB(db_path).list_clips()}
    except Exception:  # noqa: BLE001 - a locked or older database: the folders still tell most of it
        return {}


def _clip(d: Path, row: dict[str, Any] | None) -> dict[str, Any] | None:
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    seconds = meta.get("seconds")
    if seconds is None and meta.get("end_s") is not None and meta.get("start_s") is not None:
        seconds = round(float(meta["end_s"]) - float(meta["start_s"]), 1)
    files = {f: (d / f).stat().st_size for f in FILES if (d / f).exists()}
    judge = meta.get("judge_source") or "none"
    return {
        "id": d.name,
        "streamer": meta.get("streamer") or "",
        "platform": _platform(meta),
        "title": meta.get("title") or "",
        "hook": meta.get("hook") or "",
        "description": meta.get("description") or "",
        "lane": meta.get("lane") or ("vod" if meta.get("vod_id") else "live"),
        "seconds": seconds,
        "chat_score": meta.get("chat_score"),
        "final_score": meta.get("final_score"),
        "score": meta.get("score"),
        "loud_z": meta.get("loud_z"),
        "viewer_clip": bool(meta.get("viewer_clip")),
        "chatters": meta.get("unique_chatters"),
        "chat_rank": meta.get("chat_rank"),
        "chat_pct": meta.get("chat_pct"),
        "captions": meta.get("captions") or "",
        "layout": meta.get("layout") or "",
        "credit": meta.get("credit") or "",
        "judge": {
            "source": judge,
            "score": meta.get("judge_score"),
            "why": meta.get("why") or "",
            "flags": meta.get("flags") or {},
        },
        "signals": meta.get("signals") or {},
        # the steps; the lane's own total and the posting of the card are not steps of work
        "timings": {k: v for k, v in (meta.get("timings") or {}).items() if k not in ("total", "post")},
        "render_layout": meta.get("render_layout") or "",
        "recipe": (d / "recipe.json").is_file(),
        "state": (row or {}).get("state") or meta.get("state") or "READY",
        "approved_by": (row or {}).get("approved_by"),
        "youtube_id": (row or {}).get("youtube_id"),
        "created": (row or {}).get("created_at") or (d / "meta.json").stat().st_mtime,
        "files": files,
    }


def clips(root: Path | None = None) -> list[dict[str, Any]]:
    """Every staged clip, newest first."""
    root = Path(root) if root else staging_root()
    if not root.is_dir():
        return []
    rows = _states(root)
    out = [c for d in root.iterdir() if (d / "meta.json").is_file() and (c := _clip(d, rows.get(d.name)))]
    return sorted(out, key=lambda c: -float(c["created"] or 0))


def clip(clip_id: str, root: Path | None = None) -> dict[str, Any] | None:
    if not CLIP_ID.fullmatch(clip_id):
        return None
    root = Path(root) if root else staging_root()
    d = root / clip_id
    return _clip(d, _states(root).get(clip_id)) if (d / "meta.json").is_file() else None


def media_path(clip_id: str, name: str, root: Path | None = None) -> Path | None:
    """The file of a staged clip, or None: only the two videos of a clip folder are ever served."""
    if name not in FILES or not CLIP_ID.fullmatch(clip_id):
        return None
    p = (Path(root) if root else staging_root()) / clip_id / name
    return p if p.is_file() else None


def own_streamers() -> dict[str, Any]:
    """The streamers of config/streamers.yaml. Without that file the bot falls back on the example file, which
    is a template to copy and not a list of channels to offer."""
    return config.streamers() if (config.CONFIG_DIR / "streamers.yaml").is_file() else {}


def _tool(path: str) -> bool:
    return shutil.which(path) is not None or (config.ROOT / path).is_file()


def overview() -> dict[str, Any]:
    g = config.global_config()
    j = g.get("judge", {})
    streamers = [
        {
            "login": k,
            "platform": v.get("platform", "twitch"),
            "language": v.get("language"),
            "url": v.get("url"),
        }
        for k, v in own_streamers().items()
    ]
    return {
        "dry_run": config.dry_run(),
        "judge_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        # captions need the words: without a speech model clips are rendered without them
        "speech": importlib.util.find_spec("faster_whisper") is not None,
        "staging": str(staging_root()),
        # what a lane needs beyond the project itself
        "tools": {"twitch_vod": _tool(config.twitch_downloader_bin())},
        "streamers": streamers,
        "language": config.channel_language(),
        "captions": [
            {"id": s, "name": name, "about": about}
            for s, (name, about) in CAPTIONS.items()
            if s in CAPTION_STYLES
        ],
        "caption_default": str(g.get("render", {}).get("captions", "word")),
        "judge": {
            "mode": j.get("mode", "single"),
            "judge_model": j.get("judge_model"),
            "screen_model": j.get("screen_model"),
            "effort": j.get("effort"),
            "frames": j.get("frames"),
            "frames_max": j.get("frames_max"),
        },
        "clip": {
            "min_s": g.get("render", {}).get("clip_min_s"),
            "max_s": g.get("render", {}).get("clip_max_s"),
        },
        "steps": STEP_NAMES,
    }


def _day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def insights(root: Path | None = None, runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The four questions of the Insights page, answered from what is on disk."""
    cs = clips(root)
    # speed: what each step costs, over the clips that carry timings
    timed = [c for c in cs if c["timings"]]
    steps: dict[str, list[float]] = defaultdict(list)
    for c in timed:
        for k, v in c["timings"].items():
            steps[k].append(float(v))
    order = [k for k in STEP_ORDER if k in steps] + sorted(k for k in steps if k not in STEP_ORDER)
    per_clip = [
        {
            "id": c["id"],
            "streamer": c["streamer"],
            "work_s": round(sum(float(v) for k, v in c["timings"].items() if k != "wait"), 2),
            "clip_s": c["seconds"],
        }
        for c in timed
    ]
    tests = [r for r in (runs or []) if r.get("kind") == "tests" and r.get("tests")]
    speed = {
        "clips": len(timed),
        "steps": [
            {
                "step": k,
                "name": STEP_NAMES.get(k, k),
                "avg_s": round(sum(steps[k]) / len(steps[k]), 2),
                "max_s": round(max(steps[k]), 2),
                "n": len(steps[k]),
            }
            for k in order
        ],
        "per_clip": per_clip,
        "avg_work_s": round(sum(p["work_s"] for p in per_clip) / len(per_clip), 2) if per_clip else None,
        # every run the app started, newest first
        "runs": [
            {
                "id": r.get("id"),
                "kind": r.get("kind"),
                "state": r.get("state"),
                "when": r.get("started"),
                "seconds": r.get("seconds"),
                "clips": len(r.get("staged") or []),
                "what": str(
                    (r.get("params") or {}).get("streamer") or (r.get("params") or {}).get("clip") or ""
                ),
            }
            for r in (runs or [])[:60]
        ],
        "tests": [
            {
                "when": r.get("started"),
                "seconds": r["tests"].get("seconds"),
                "passed": r["tests"].get("passed", 0),
                "failed": r["tests"].get("failed", 0),
            }
            for r in tests
        ][-20:],
    }
    # cost: the ledger
    rows = ledger.read()
    paid = [r for r in rows if not r.get("error")]
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"usd": 0.0, "calls": 0})
    by_model: dict[str, dict[str, float]] = defaultdict(lambda: {"usd": 0.0, "calls": 0})
    parts: Counter[str] = Counter()
    tokens: Counter[str] = Counter()
    for r in paid:
        usd = float(r.get("usd") or 0)
        for bucket in (by_day[_day(r["t"])], by_model[r.get("model") or "?"]):
            bucket["usd"] += usd
            bucket["calls"] += 1
        parts.update({k: float(v) for k, v in (r.get("usd_parts") or {}).items()})
        tokens.update({k: int(r.get(k) or 0) for k in ledger.TOKENS})
    j = config.global_config().get("judge", {})
    prompt = config.ROOT / "prompts" / "judge_v2.md"
    cost = {
        "usd": round(sum(float(r.get("usd") or 0) for r in paid), 6),
        "calls": len(paid),
        "failed": len(rows) - len(paid),
        "unpriced": sum(1 for r in paid if r.get("usd") is None),
        "by_day": [
            {"day": d, "usd": round(v["usd"], 6), "calls": int(v["calls"])} for d, v in sorted(by_day.items())
        ],
        "by_model": [
            {"model": m, "usd": round(v["usd"], 6), "calls": int(v["calls"])}
            for m, v in sorted(by_model.items())
        ],
        "by_kind": {k: round(v, 6) for k, v in parts.items()},
        "tokens": dict(tokens),
        "prices": j.get("prices") or {},
        "judge_model": j.get("judge_model"),
        "screen_model": j.get("screen_model"),
        # for the estimate, while there is little or nothing to go by
        "assume": {
            "system_tokens": round(len(prompt.read_text(encoding="utf-8")) / 3.5) if prompt.exists() else 0,
            "frame_tokens": 173,  # a 480x270 frame: width x height / 750
            "frames": j.get("frames", 8),
            "frames_max": j.get("frames_max", 16),
        },
    }
    # judge: who wrote each clip's title, and what the judge decided call by call
    sources = Counter(c["judge"]["source"] for c in cs)
    decisions = [
        {
            "t": r["t"],
            "streamer": r.get("streamer"),
            "task": r.get("task"),
            "ok": r.get("ok"),
            "score": r.get("score"),
            "title": r.get("title"),
            "why": r.get("why"),
            "flags": r.get("flags"),
            "error": r.get("error"),
            "usd": r.get("usd"),
        }
        for r in rows
        if r.get("task") != "legacy"
    ][-200:]
    judge = {
        "sources": dict(sources),
        "decisions": decisions[::-1],
        "accepted": sum(1 for d in decisions if d["ok"] is True),
        "rejected": sum(1 for d in decisions if d["ok"] is False),
        "errors": sum(1 for d in decisions if d["error"]),
        "scores": dict(Counter(int(d["score"]) for d in decisions if isinstance(d["score"], (int, float)))),
    }
    # detector: what the staged moments looked like
    with_sig = [c for c in cs if c["signals"]]
    avg_sig = (
        {
            k: round(sum(float(c["signals"].get(k) or 0) for c in with_sig) / len(with_sig), 2)
            for k in ("zU", "zM", "zE", "zS")
        }
        if with_sig
        else {}
    )
    by_streamer = Counter(c["streamer"] for c in cs)
    detector = {
        "clips": len(cs),
        "by_streamer": [
            {"streamer": s, "platform": next(c["platform"] for c in cs if c["streamer"] == s), "clips": n}
            for s, n in by_streamer.most_common()
        ],
        "by_day": [
            {"day": d, "clips": n} for d, n in sorted(Counter(_day(float(c["created"])) for c in cs).items())
        ],
        "scores": [
            {
                "id": c["id"],
                "streamer": c["streamer"],
                "chat": c["chat_score"],
                "final": c["final_score"],
                "chatters": c["chatters"],
            }
            for c in cs
            if c["chat_score"] is not None
        ],
        "signals": avg_sig,
        "signal_clips": len(with_sig),
        "loud": sum(1 for c in cs if c["loud_z"] is not None),
        "viewer_clip": sum(1 for c in cs if c["viewer_clip"]),
        "weights": config.global_config().get("detector", {}).get("weights") or {},
    }
    return {"speed": speed, "cost": cost, "judge": judge, "detector": detector}
