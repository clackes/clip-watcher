"""Who the streamers are, for the page: display name, profile picture, badges.

Asked of the platforms without a login, as the bot's other anonymous reads are (Twitch's GQL, Kick's channel
address), then kept on disk and served from there: the page never loads a picture from another site. A
profile is asked again after a day; a picture or badge image is downloaded once per address.

    work/ui/profiles/<platform>/<login>.json   the profile, with the addresses its images came from
    work/ui/profiles/images/<hash>             a picture or badge, named by the hash of its address
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from clipwatcher import config

PLATFORMS = ("twitch", "kick")
LOGIN = re.compile(r"[a-z0-9_]{2,40}")
IMAGE_ID = re.compile(r"[0-9a-f]{20}")
TTL_S = 24 * 3600.0
RETRY_S = 600.0  # after a lookup that failed
MAX_IMAGE = 2_000_000
MAX_BADGES = 4
# pictures are only ever downloaded from the platforms' own image hosts
IMAGE_HOSTS = ("static-cdn.jtvnw.net", "files.kick.com")
_TYPES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
)
_TWITCH_USER = (
    "query($l:String!){user(login:$l){login displayName profileImageURL(width:150) primaryColorHex "
    "roles{isPartner isAffiliate} followers{totalCount} "
    "displayBadges{setID version title imageURL(size:DOUBLE)}}}"
)
_TWITCH_BADGES = "query{badges{setID version title imageURL(size:DOUBLE)}}"

Fetch = Callable[[str], dict[str, Any]]


def profiles_root() -> Path:
    env = os.environ.get("CLIPWATCHER_UI_WORK")
    return (Path(env).expanduser() if env else config.ROOT / "work" / "ui") / "profiles"


def image_type(data: bytes) -> str | None:
    """The kind of picture the bytes are, by how they begin (never by name); None for anything else."""
    for magic, ctype in _TYPES:
        if data.startswith(magic):
            return ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _allowed(url: str) -> bool:
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in IMAGE_HOSTS)


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - https on the platforms' image hosts only
        return r.read(MAX_IMAGE + 1)


def _twitch(login: str) -> dict[str, Any]:
    from clipwatcher.chat import twitch_replay

    user = (twitch_replay._post({"query": _TWITCH_USER, "variables": {"l": login}}).get("data") or {}).get(
        "user"
    )
    if not user:
        raise LookupError(f"Twitch has no channel {login}")
    return user


def _twitch_partner_badge() -> dict[str, Any] | None:
    from clipwatcher.chat import twitch_replay

    rows = (twitch_replay._post({"query": _TWITCH_BADGES}).get("data") or {}).get("badges") or []
    return next((b for b in rows if b.get("setID") == "partner"), None)


def _kick(slug: str) -> dict[str, Any]:
    from clipwatcher.chat import kick

    return kick._http_json(f"{kick.API}/channels/{slug}")


def from_twitch(login: str, user: dict[str, Any], partner_badge: dict[str, Any] | None) -> dict[str, Any]:
    """Twitch's answer as a profile. The badges are the ones the streamer displays; a partner who displays
    another badge still gets the Verified one first."""
    roles = user.get("roles") or {}
    shown = [b for b in user.get("displayBadges") or [] if b and b.get("imageURL")]
    if roles.get("isPartner") and partner_badge and not any(b.get("setID") == "partner" for b in shown):
        shown.insert(0, partner_badge)
    shown.sort(key=lambda b: b.get("setID") != "partner")
    colour = str(user.get("primaryColorHex") or "")
    return {
        "platform": "twitch",
        "login": login,
        "name": user.get("displayName") or login,
        "avatar_url": user.get("profileImageURL"),
        "verified": bool(roles.get("isPartner")),
        "affiliate": bool(roles.get("isAffiliate")),
        "followers": (user.get("followers") or {}).get("totalCount"),
        "colour": f"#{colour}" if re.fullmatch(r"[0-9A-Fa-f]{6}", colour) else None,
        "badges": [
            {"id": str(b.get("setID")), "title": str(b.get("title") or b.get("setID")), "url": b["imageURL"]}
            for b in shown[:MAX_BADGES]
        ],
    }


def from_kick(slug: str, channel: dict[str, Any]) -> dict[str, Any]:
    """Kick's answer as a profile. Kick has no picture for "verified" (the page draws the tick); the
    channel's own badges are the ones its subscribers earn, longest first."""
    user = channel.get("user") or {}
    subs = sorted(
        (b for b in channel.get("subscriber_badges") or [] if (b.get("badge_image") or {}).get("src")),
        key=lambda b: -int(b.get("months") or 0),
    )
    badges: list[dict[str, Any]] = []
    if channel.get("verified"):
        badges.append({"id": "verified", "title": "Verified on Kick", "url": None})
    for b in subs[: MAX_BADGES - len(badges)]:
        months = int(b.get("months") or 0)
        title = f"Channel badge, {months} {'month' if months == 1 else 'months'}"
        badges.append({"id": f"sub-{months}", "title": title, "url": b["badge_image"]["src"]})
    return {
        "platform": "kick",
        "login": slug,
        "name": user.get("username") or slug,
        "avatar_url": user.get("profile_pic"),
        "verified": bool(channel.get("verified")),
        "affiliate": bool(channel.get("is_affiliate")),
        "followers": channel.get("followers_count"),
        "colour": None,
        "badges": badges,
    }


class Profiles:
    def __init__(
        self,
        root: Path | None = None,
        *,
        twitch: Fetch = _twitch,
        kick: Fetch = _kick,
        partner_badge: Callable[[], dict[str, Any] | None] = _twitch_partner_badge,
        download: Callable[[str], bytes] = _download,
        now: Callable[[], float] = time.time,
    ):
        self.root = Path(root) if root else profiles_root()
        self.twitch, self.kick, self.partner_badge, self.download, self.now = (
            twitch,
            kick,
            partner_badge,
            download,
            now,
        )
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._failed: dict[str, float] = {}
        self._partner: tuple[float, dict[str, Any] | None] | None = None

    # -- what the page gets --------------------------------------------------------------------------------
    def get(self, platform: str, login: str) -> dict[str, Any]:
        """The profile of one streamer, from disk while it is fresh, else asked again. A lookup that fails
        gives the last profile known, or a bare one (the page then shows the initial)."""
        login = login.lower()
        if platform not in PLATFORMS or not LOGIN.fullmatch(login):
            raise ValueError("platform:login, as in twitch:clix")
        key = f"{platform}:{login}"
        with self._lock(key):
            kept = self._read(platform, login)
            fresh = kept is not None and self.now() - float(kept.get("fetched") or 0) < TTL_S
            if not fresh and self.now() - self._failed.get(key, 0.0) >= RETRY_S:
                try:
                    kept = self._ask(platform, login)
                    self._failed.pop(key, None)
                except Exception:  # noqa: BLE001 - offline, a renamed channel, a changed answer: keep what we had
                    self._failed[key] = self.now()
            return self._public(kept or {"platform": platform, "login": login, "name": login, "badges": []})

    def many(self, who: list[tuple[str, str]]) -> list[dict[str, Any]]:
        if not who:
            return []
        with ThreadPoolExecutor(max_workers=6) as pool:
            return list(pool.map(lambda pair: self.get(*pair), who))

    def image(self, image_id: str) -> tuple[Path, str] | None:
        """A kept picture and its type, by the id the profile names it with."""
        if not IMAGE_ID.fullmatch(image_id):
            return None
        path = self.root / "images" / image_id
        try:
            with path.open("rb") as f:
                ctype = image_type(f.read(16))
        except OSError:
            return None
        return (path, ctype) if ctype else None

    # -- asking and keeping --------------------------------------------------------------------------------
    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _path(self, platform: str, login: str) -> Path:
        return self.root / platform / f"{login}.json"

    def _read(self, platform: str, login: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(platform, login).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _ask(self, platform: str, login: str) -> dict[str, Any]:
        if platform == "twitch":
            user = self.twitch(login)
            partner = self._partner_badge() if (user.get("roles") or {}).get("isPartner") else None
            prof = from_twitch(login, user, partner)
        else:
            prof = from_kick(login, self.kick(login))
        prof["avatar"] = self._keep_image(prof.get("avatar_url"))
        for b in prof["badges"]:
            b["image"] = self._keep_image(b.get("url"))
        prof["fetched"] = self.now()
        path = self._path(platform, login)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(prof, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return prof

    def _partner_badge(self) -> dict[str, Any] | None:
        """Twitch's Verified badge, looked up once a day (a partner may display another badge instead)."""
        if self._partner is None or self.now() - self._partner[0] >= TTL_S:
            try:
                self._partner = (self.now(), self.partner_badge())
            except Exception:  # noqa: BLE001 - the profile is still worth having without it
                self._partner = (self.now() - TTL_S + RETRY_S, None)
        return self._partner[1]

    def _keep_image(self, url: str | None) -> str | None:
        """Download a picture once and return the id it is kept under; None when it cannot be had."""
        if not url or not _allowed(url):
            return None
        image_id = hashlib.sha256(url.encode()).hexdigest()[:20]
        path = self.root / "images" / image_id
        if path.is_file():
            return image_id
        try:
            data = self.download(url)
        except Exception:  # noqa: BLE001 - a picture that cannot be fetched is a missing picture
            return None
        if len(data) > MAX_IMAGE or image_type(data) is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return image_id

    @staticmethod
    def _public(prof: dict[str, Any]) -> dict[str, Any]:
        def src(image_id: str | None) -> str | None:
            return f"/profile/image/{image_id}" if image_id else None

        return {
            "platform": prof["platform"],
            "login": prof["login"],
            "name": prof.get("name") or prof["login"],
            "avatar": src(prof.get("avatar")),
            "verified": bool(prof.get("verified")),
            "affiliate": bool(prof.get("affiliate")),
            "followers": prof.get("followers"),
            "colour": prof.get("colour"),
            "badges": [
                {"id": b.get("id"), "title": b.get("title"), "image": src(b.get("image"))}
                for b in prof.get("badges") or []
                if b.get("image") or b.get("id") == "verified"
            ],
            "known": bool(prof.get("fetched")),
        }
