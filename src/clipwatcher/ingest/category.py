"""What a live stream is playing right now, from streamlink's stream metadata (no API keys needed).

The channel posts GTA 6 clips only (owner, 19 Nov): `channel.games` in global.yaml lists the categories a
moment may come from; moments from any other category are dropped before cutting.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable

from .. import config

TTL_S = 120.0  # a stream's category rarely changes; at most one streamlink lookup per streamer per 2 min
_CACHE: dict[str, tuple[float, str | None]] = {}


def stream_url(platform: str, login: str) -> str:
    return f"https://kick.com/{login}" if platform == "kick" else f"https://www.twitch.tv/{login}"


def stream_category(
    platform: str, login: str, *, run: Callable = subprocess.run, now: Callable[[], float] = time.time
) -> str | None:
    """The live category ("Grand Theft Auto VI"), "" when the stream reports none, None when unknown."""
    key = f"{platform}:{login.lower()}"
    hit = _CACHE.get(key)
    if hit and now() - hit[0] < TTL_S:
        return hit[1]
    try:
        r = run(
            [config.streamlink_bin(), "--json", stream_url(platform, login)],
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        data = json.loads(r.stdout or "{}")
        cat = None if "error" in data else str((data.get("metadata") or {}).get("category") or "")
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        cat = None
    _CACHE[key] = (now(), cat)
    return cat


def allowed(category: str | None, games: list[str]) -> bool:
    """No filter, or the lookup failed (the judge still sees the frames): let it through."""
    if not games or category is None:
        return True
    return category.casefold() in {g.casefold() for g in games}
