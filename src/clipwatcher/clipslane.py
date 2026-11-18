"""Clips lane: viewers already clip the best moments, so the bot edits their top clips into Shorts.

Every run, for each English streamer in streamers.yaml:
1. list the most-viewed viewer clips of the last day (Twitch Helix with TWITCH_CLIENT_ID/SECRET, Kick's
   public channel API);
2. skip clips already seen, too short, too long or under clips_lane.min_views;
3. download the clip (TwitchDownloaderCLI / ffmpeg), find its loudest moment;
4. run it through the VOD lane's chain: transcript, judge, trim, jump cuts, beats, layout, render;
5. stage only what the judge approves and post the Telegram approval card.

CLI: python -m clipwatcher.clipslane [--streamers a,b] [--once] [--period day|week]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipwatcher import config
from clipwatcher.models import Candidate

log = logging.getLogger(__name__)

KICK_CLIPS = "https://kick.com/api/v2/channels/{slug}/clips?{q}"
HELIX = "https://api.twitch.tv/helix"


@dataclass
class SourceClip:
    platform: str  # twitch | kick
    streamer: str
    id: str
    title: str
    views: int
    duration: float
    created_s: float
    url: str  # public page
    media: str  # what the downloader takes: Twitch clip id, Kick HLS/MP4 url


def settings() -> dict[str, Any]:
    d = {"min_views": {"twitch": 200, "kick": 100}, "per_streamer": 3, "min_s": 12, "max_s": 90,
         "max_per_run": 12, "interval_min": 60, "period": "week"}  # fmt: skip
    d.update(config.global_config().get("clips_lane", {}) or {})
    return d


def _iso(s: str) -> float:
    from datetime import datetime

    s = s.replace("Z", "+00:00")
    if "." in s:  # Kick sends 6+ fractional digits; fromisoformat wants at most 6
        head, rest = s.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())[:6]
        tz = rest[len("".join(ch for ch in rest if ch.isdigit())) :]
        s = f"{head}.{frac}{tz}"
    return datetime.fromisoformat(s).timestamp()


def _get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 20.0) -> Any:
    import urllib.request

    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https hosts)
        return json.loads(r.read().decode())


# -- sources -------------------------------------------------------------------------------------------
def parse_kick(payload: dict[str, Any], slug: str) -> list[SourceClip]:
    out = []
    for c in payload.get("clips") or []:
        media = c.get("video_url") or c.get("clip_url")
        if not media or c.get("privacy", "public") != "public":
            continue
        out.append(
            SourceClip(
                "kick",
                slug,
                str(c["id"]),
                str(c.get("title") or "").strip(),
                int(c.get("views") or c.get("view_count") or 0),
                float(c.get("duration") or 0),
                _iso(c["created_at"]),
                f"https://kick.com/{slug}/clips/{c['id']}",
                media,
            )  # fmt: skip
        )
    return out


def kick_top(slug: str, period: str = "day", get=_get_json) -> list[SourceClip]:
    q = urllib.parse.urlencode({"sort": "view", "time": period})
    return parse_kick(get(KICK_CLIPS.format(slug=slug, q=q)), slug)


def parse_twitch(payload: dict[str, Any], login: str) -> list[SourceClip]:
    return [
        SourceClip(
            "twitch",
            login,
            str(c["id"]),
            str(c.get("title") or "").strip(),
            int(c.get("view_count") or 0),
            float(c.get("duration") or 0),
            _iso(c["created_at"]),
            str(c.get("url") or ""),
            str(c["id"]),
        )  # fmt: skip
        for c in payload.get("data") or []
    ]


class Twitch:
    """Helix with an app token (client credentials). Disabled when TWITCH_CLIENT_ID/SECRET are unset."""

    def __init__(self, get=_get_json) -> None:
        self.cid = os.environ.get("TWITCH_CLIENT_ID", "")
        self.secret = os.environ.get("TWITCH_CLIENT_SECRET", "")
        self.get = get
        self._token = ""
        self._ids: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.cid and self.secret)

    def _h(self) -> dict[str, str]:
        if not self._token:
            from clipwatcher.detect.viewer_clips import fetch_app_token

            self._token = fetch_app_token(self.cid, self.secret)
        return {"Client-Id": self.cid, "Authorization": f"Bearer {self._token}"}

    def top(self, login: str, since_s: float, first: int = 20) -> list[SourceClip]:
        if login not in self._ids:
            users = self.get(f"{HELIX}/users?login={urllib.parse.quote(login)}", self._h()).get("data") or []
            if not users:
                return []
            self._ids[login] = users[0]["id"]
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since_s))
        q = urllib.parse.urlencode({"broadcaster_id": self._ids[login], "first": first, "started_at": since})
        return parse_twitch(self.get(f"{HELIX}/clips?{q}", self._h()), login)


def select(clips: list[SourceClip], seen: set[str], s: dict[str, Any] | None = None) -> list[SourceClip]:
    """Unseen clips with enough views and a usable length, most viewed first, per_streamer at most."""
    s = s or settings()
    mv = s["min_views"]
    ok = [
        c
        for c in clips
        if c.id not in seen
        and c.views >= int(mv.get(c.platform, 0) if isinstance(mv, dict) else mv)
        and float(s["min_s"]) <= c.duration <= float(s["max_s"])
    ]
    return sorted(ok, key=lambda c: -c.views)[: int(s["per_streamer"])]


# -- state ---------------------------------------------------------------------------------------------
class Seen:
    """Clip ids already processed (judged, rendered or rejected), with the time, in one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        try:
            self.d: dict[str, float] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.d = {}

    def ids(self) -> set[str]:
        return set(self.d)

    def add(self, clip_id: str) -> None:
        self.d[clip_id] = time.time()
        cutoff = time.time() - 30 * 86400  # forget after a month: the APIs only list recent clips
        self.d = {k: v for k, v in self.d.items() if v >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d))
        tmp.replace(self.path)


# -- download + process --------------------------------------------------------------------------------
def download(c: SourceClip, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    if c.platform == "twitch":
        cmd = [config.twitch_downloader_bin(), "clipdownload", "-u", c.media, "-o", str(out), "-q", "1080p60"]
        if subprocess.run(cmd, capture_output=True, timeout=300).returncode != 0:  # 1080p60 not offered
            subprocess.run(cmd[:-2], capture_output=True, timeout=300, check=True)
    else:
        subprocess.run(
            [config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", c.media, "-c", "copy",
             "-bsf:a", "aac_adtstoasc", "-t", "200", str(out)],
            capture_output=True, timeout=300, check=True,
        )  # fmt: skip
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"download failed: {c.url}")
    return out


def candidate_for(c: SourceClip, media: Path, dur: float) -> Candidate:
    """The whole clip as the judge window; the loudest moment stands in for the chat peak."""
    from clipwatcher.render import cold_open

    loud = cold_open.loud_moments(cold_open.loudness(media, 0.0, dur))
    peak = max(loud, key=lambda x: x[1])[0] if loud else dur * 0.5
    return Candidate(stream=c.streamer, peak_s=peak, start_s=0.0, end_s=dur, chat_score=0.0, final_score=0.0)


def process(c: SourceClip, *, work: Path, staging_root: Path) -> Path | None:
    from clipwatcher import vod
    from clipwatcher.render.template import probe

    media = download(c, work / f"{c.platform}-{c.id}.mp4")
    try:
        dur = float(
            subprocess.run(
                [config.ffprobe_bin(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                 str(media)], capture_output=True, text=True, check=True,
            ).stdout.strip()
        )  # fmt: skip
        probe(media)  # fail early on a broken download
        cand = candidate_for(c, media, dur)
        return vod.process_candidate(
            0, cand, streamer=c.streamer, vod=c.id, vid=c.id.replace("_", "")[-12:], work=work,
            out_dir=staging_root, video=media, messages=[], language=config.stream_language(c.streamer),
            judge=True, pad_s=0.0, quality="1080p60", platform=c.platform, skip_rejected=True, views=c.views,
            extra_meta={"lane": "clips", "source_clip": c.url, "source_views": c.views,
                        "source_title": c.title},
        )  # fmt: skip
    finally:
        media.unlink(missing_ok=True)


# -- run -----------------------------------------------------------------------------------------------
def gather(streamers: list[str], *, period: str, twitch: Twitch | None = None) -> list[SourceClip]:
    twitch = twitch or Twitch()
    since = time.time() - (7 if period == "week" else 1) * 86400
    out: list[SourceClip] = []
    for name in streamers:
        plat = config.streamer(name).get("platform", "twitch")
        try:
            if plat == "kick":
                out += kick_top(name, period)
            elif twitch.enabled:
                out += twitch.top(name, since)
        except Exception as e:  # noqa: BLE001 - one channel failing never stops the run
            log.warning("%s (%s): listing clips failed: %s", name, plat, e)
    return out


def run_once(
    streamers: list[str], *, staging_root: Path, work: Path, period: str | None = None, post=None
) -> list[str]:
    """One pass; returns the staged clip ids. post(clip_id, short_path) posts the Telegram card."""
    s = settings()
    seen = Seen(staging_root / "clips_lane_seen.json")
    clips = gather(streamers, period=period or s["period"])
    picks = [c for name in streamers for c in select([c for c in clips if c.streamer == name], seen.ids(), s)]
    picks = sorted(picks, key=lambda c: -c.views)[: int(s["max_per_run"])]
    log.info("clips lane: %d listed, %d to judge", len(clips), len(picks))
    staged = []
    for c in picks:
        try:
            pkg = process(c, work=work, staging_root=staging_root)
        except Exception as e:  # noqa: BLE001
            log.warning("%s %s: %s", c.platform, c.id, e)
            pkg = None
        seen.add(c.id)  # judged or failed: never paid for twice
        if pkg is not None:
            staged.append(pkg.name)
            log.info("staged %s (%s views): %s", pkg.name, c.views, c.title)
            if post:
                post(pkg.name, pkg / "short_9x16.mp4")
    return staged


def _poster(staging_root: Path):
    """Telegram card poster for staged clips, or None without TELEGRAM_BOT_TOKEN."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return None
    from clipwatcher.queue.db import ClipDB
    from clipwatcher.queue.telegram_bot import build_application, post_card

    db = ClipDB(staging_root / "clips.db")
    app = build_application(db)

    def post(clip_id: str, short: Path) -> None:
        async def go() -> None:
            await app.initialize()
            try:
                await post_card(app, db, clip_id, short)
            finally:
                await app.shutdown()

        asyncio.run(go())

    return post


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipwatcher.clipslane")
    ap.add_argument("--streamers", help="comma-separated (default: every English streamer in streamers.yaml)")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--period", choices=["day", "week"])
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    names = [n.strip().lower() for n in (a.streamers or ",".join(config.streamers())).split(",") if n.strip()]
    names = [n for n in names if config.allowed_streamer(n)]
    if not Twitch().enabled:
        log.warning("TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET not set: Twitch clips skipped, Kick only")
    staging_root = Path(os.environ.get("CLIPWATCHER_STAGING", config.ROOT / "staging"))
    work = Path(os.environ.get("CLIPWATCHER_DATA", config.ROOT / "work")) / "clips_lane"
    post = _poster(staging_root)
    while True:
        run_once(names, staging_root=staging_root, work=work, period=a.period, post=post)
        if a.once:
            return 0
        time.sleep(float(settings()["interval_min"]) * 60)


if __name__ == "__main__":
    raise SystemExit(main())
