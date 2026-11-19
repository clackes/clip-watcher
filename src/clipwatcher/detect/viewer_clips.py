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


@dataclass(frozen=True)
class ViewerClip:
    id: str
    created_s: float  # epoch seconds
    url: str = ""

    @property
    def window(self) -> tuple[float, float]:
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
