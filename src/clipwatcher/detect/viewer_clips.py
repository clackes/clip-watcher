"""Viewer-made Twitch clips as a confirmation signal: poll Helix Get Clips for the current stream.

A clip created at T covers roughly T-35 s .. T of the stream (the moments just before the click).
Times are epoch seconds so they compare directly with live Candidates.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from clipwatcher.detect.detector import confirm as _confirm
from clipwatcher.models import Candidate

HELIX_CLIPS = "https://api.twitch.tv/helix/clips"
CLIP_LOOKBACK_S = 35.0


KICK_CLIPS = "https://kick.com/api/v2/channels/{slug}/clips"
KICK_EDIT_S = 30.0  # a Kick clip is cut from the last minutes of the stream and saved a little later


@dataclass(frozen=True)
class ViewerClip:
    id: str
    created_s: float  # epoch seconds
    url: str = ""
    duration_s: float | None = None  # Kick says how long the clip is (13-180 s); Twitch does not

    @property
    def window(self) -> tuple[float, float]:
        """The stretch of the stream the clip covers, as well as it is known."""
        if self.duration_s:
            return self.created_s - self.duration_s - KICK_EDIT_S, self.created_s
        return clip_window(self.created_s)

    @property
    def moment_s(self) -> float:
        a, b = self.window
        return (a + b) / 2


def parse_rfc3339(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def to_rfc3339(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clip_window(created_s: float, lookback_s: float = CLIP_LOOKBACK_S) -> tuple[float, float]:
    return created_s - lookback_s, created_s


def parse_clips(payload: dict[str, Any]) -> list[ViewerClip]:
    out = []
    for c in payload.get("data", []):
        try:
            out.append(ViewerClip(str(c["id"]), parse_rfc3339(c["created_at"]), c.get("url", "")))
        except (KeyError, ValueError):
            continue
    return out


def fetch_app_token(client_id: str, client_secret: str) -> str:
    body = urllib.parse.urlencode(
        {"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}
    ).encode()
    with urllib.request.urlopen("https://id.twitch.tv/oauth2/token", data=body, timeout=15) as r:  # noqa: S310
        return json.loads(r.read())["access_token"]


def _helix_get(url: str, client_id: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - fixed https endpoint
        return json.loads(r.read())


class ViewerClipPoller:
    """poll_once() -> new clips since the last call; run(cb) loops every ``interval`` s."""

    def __init__(
        self,
        broadcaster_id: str,
        started_at: float,
        *,
        client_id: str | None = None,
        token: str | None = None,
        interval: float = 60.0,
        get: Callable[[str], dict[str, Any]] | None = None,
    ):
        self.broadcaster_id, self.started_at, self.interval = broadcaster_id, started_at, interval
        self.client_id = client_id or os.environ.get("TWITCH_CLIENT_ID", "")
        self._token = token
        self._get = get
        self.seen: dict[str, ViewerClip] = {}

    def url(self) -> str:
        q = {"broadcaster_id": self.broadcaster_id, "started_at": to_rfc3339(self.started_at), "first": "100"}
        return f"{HELIX_CLIPS}?{urllib.parse.urlencode(q)}"

    def _request(self) -> dict[str, Any]:
        if self._get:
            return self._get(self.url())
        if not self._token:
            self._token = fetch_app_token(self.client_id, os.environ.get("TWITCH_CLIENT_SECRET", ""))
        return _helix_get(self.url(), self.client_id, self._token)

    def poll_once(self) -> list[ViewerClip]:
        new = [c for c in parse_clips(self._request()) if c.id not in self.seen]
        for c in new:
            self.seen[c.id] = c
        return new

    async def run(self, on_clip: Callable[[ViewerClip], None]) -> None:
        while True:
            try:
                for c in await asyncio.to_thread(self.poll_once):
                    on_clip(c)
            except (OSError, ValueError, KeyError):
                pass  # transient API error: retry next cycle
            await asyncio.sleep(self.interval)

    def moment_times(self) -> list[float]:
        return [c.moment_s for c in self.seen.values()]


class KickClipPoller:
    """Viewer-made clips of a Kick broadcast. Kick lists a channel's clips, newest first, to anyone:
    no login and no key. Same interface as ViewerClipPoller (``seen``, ``poll_once``, ``run``)."""

    def __init__(
        self,
        slug: str,
        livestream_id: str | None = None,
        *,
        interval: float = 60.0,
        get: Callable[[str], dict[str, Any]] | None = None,
    ):
        self.slug, self.livestream_id, self.interval = slug.lower(), livestream_id, interval
        self._get = get
        self.seen: dict[str, ViewerClip] = {}

    def _request(self) -> dict[str, Any]:
        url = KICK_CLIPS.format(slug=self.slug) + "?cursor=0&sort=date&time=day"
        if self._get:
            return self._get(url)
        from clipwatcher.chat import kick

        return kick._http_json(url, {})

    def poll_once(self) -> list[ViewerClip]:
        new = []
        for c in self._request().get("clips") or []:
            try:
                if self.livestream_id and str(c.get("livestream_id")) != str(self.livestream_id):
                    continue  # a clip of an earlier broadcast
                clip = ViewerClip(
                    str(c["id"]), parse_rfc3339(c["created_at"]), c.get("clip_url") or "",
                    float(c["duration"]) if c.get("duration") else None,
                )  # fmt: skip
            except (KeyError, ValueError, TypeError):
                continue
            if clip.id not in self.seen:
                self.seen[clip.id] = clip
                new.append(clip)
        return new

    async def run(self, on_clip: Callable[[ViewerClip], None]) -> None:
        while True:
            try:
                for c in await asyncio.to_thread(self.poll_once):
                    on_clip(c)
            except Exception:  # noqa: BLE001 - blocked, offline, a changed answer: try again next cycle
                pass
            await asyncio.sleep(self.interval)

    def moment_times(self) -> list[float]:
        return [c.moment_s for c in self.seen.values()]


def kick_vod_clip_moments(
    slug: str, stream_id: str, start_ms: float, get: Callable[[str], dict[str, Any]] | None = None
) -> list[float]:
    """Where in a Kick VOD viewers made their clips: the middle of each, in seconds from the VOD start.
    The channel's most viewed clips of the last week, filtered to this broadcast. [] on any error."""
    try:
        url = KICK_CLIPS.format(slug=slug.lower()) + "?cursor=0&sort=view&time=week"
        if get is None:
            from clipwatcher.chat import kick

            d = kick._http_json(url, {})
        else:
            d = get(url)
        out = []
        for c in d.get("clips") or []:
            if str(c.get("livestream_id")) != str(stream_id) or not c.get("duration"):
                continue
            clip = ViewerClip(str(c["id"]), parse_rfc3339(c["created_at"]), "", float(c["duration"]))
            a, b = clip.window
            out.append((a + b) / 2 - start_ms / 1000)
        return out
    except Exception:  # noqa: BLE001 - blocked, offline, a changed answer: confirmation is a bonus
        return []


_VOD_CLIPS_Q = (
    "query($login: String!) { user(login: $login) { clips(first: 100, criteria: {period: LAST_WEEK,"
    " sort: VIEWS_DESC}) { edges { node { videoOffsetSeconds durationSeconds video { id } } } } } }"
)


def vod_clip_moments(
    login: str, vod_id: str, post: Callable[[dict[str, Any]], dict[str, Any]] | None = None
) -> list[float]:
    """Where in a VOD viewers made their clips: the middle of each, in seconds from the VOD start.

    The channel's 100 most viewed clips of the last week, without a login (the page Twitch shows anyone),
    so an old VOD or a channel with more clips than that returns few or none. [] on any error: confirmation
    is a bonus, never a requirement.
    """
    from clipwatcher.chat import twitch_replay

    try:
        d = (post or twitch_replay._post)({"query": _VOD_CLIPS_Q, "variables": {"login": login.lower()}})
        edges = (((d.get("data") or {}).get("user") or {}).get("clips") or {}).get("edges") or []
        return [
            float(n["videoOffsetSeconds"]) + float(n.get("durationSeconds") or 0) / 2
            for n in (e["node"] for e in edges)
            if (n.get("video") or {}).get("id") == str(vod_id) and n.get("videoOffsetSeconds") is not None
        ]
    except Exception:  # noqa: BLE001 - offline, schema change, anything
        return []


def confirm(
    candidate: Candidate,
    clips: Sequence[ViewerClip],
    loud_z: float | None = None,
    clip_tol_s: float = 30.0,
) -> Candidate:
    """Same contract as detect.detector.confirm, fed with ViewerClip objects (epoch seconds)."""
    return _confirm(candidate, [c.moment_s for c in clips], loud_z=loud_z, clip_tol_s=clip_tol_s)
