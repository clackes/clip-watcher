"""The YouTube accounts the browser publisher can post from: one Chrome profile per account, signed in once
by a person (publish/youtube_login.py), and what is known about each.

    ~/.clipwatcher/youtube/                 (or the folder named by CLIPWATCHER_ACCOUNTS)
        registry.json                       which account posts
        <account id>/account.json           channel id, name, handle, picture, subscribers, dates, state
        <account id>/profile/               its Chrome profile (the sign-in lives here)
        <account id>/status.json            what a sign-in or check that is running has reached
        images/<hash>                       channel pictures, downloaded once, named by their address's hash
    ~/.clipwatcher/yt-chrome-profile        the single sign-in of before: offered for adoption, used in place

Nothing here is a secret the project holds: the sign-in is Chrome's own, inside the profile folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ACCOUNT_ID = re.compile(r"[0-9a-f]{12}")
IMAGE_ID = re.compile(r"[0-9a-f]{20}")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
MAX_IMAGE = 2_000_000
# channel pictures are only ever downloaded from Google's own image hosts
IMAGE_HOSTS = ("ggpht.com", "googleusercontent.com")
READY, SIGN_IN, UNKNOWN = "ready", "signin", "unknown"
_TYPES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"))
_FIELDS = ("channel_id", "name", "handle", "subscribers", "added", "checked", "state")  # what the page gets
_GUARD = threading.RLock()


def accounts_root() -> Path:
    env = os.environ.get("CLIPWATCHER_ACCOUNTS")
    return Path(env).expanduser() if env else Path.home() / ".clipwatcher" / "youtube"


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
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - https on Google's image hosts only
        return r.read(MAX_IMAGE + 1)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Accounts:
    """The accounts kept under one root. Safe to use from the app and from the sign-in process at once:
    every file is written whole."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        download: Callable[[str], bytes] = _download,
        now: Callable[[], float] = time.time,
    ):
        self.root = Path(root) if root else accounts_root()
        self.download, self.now = download, now

    # -- where things are ----------------------------------------------------------------------------------
    def folder(self, account_id: str) -> Path:
        if not ACCOUNT_ID.fullmatch(account_id or ""):
            raise ValueError("No such account.")
        return self.root / account_id

    def status_path(self, account_id: str) -> Path:
        return self.folder(account_id) / "status.json"

    def profile_path(self, account_id: str) -> Path:
        """The Chrome profile of an account: its own folder, or the old single profile it adopted."""
        rec = _read_json(self.folder(account_id) / "account.json") or {}
        return Path(rec["profile"]) if rec.get("profile") else self.folder(account_id) / "profile"

    def legacy_profile(self) -> Path:
        """Where the one sign-in of before lives (scripts/yt_browser_login.py made it)."""
        return self.root.parent / "yt-chrome-profile"

    # -- reading -------------------------------------------------------------------------------------------
    def get(self, account_id: str) -> dict[str, Any] | None:
        if not ACCOUNT_ID.fullmatch(account_id or ""):
            return None
        rec = _read_json(self.root / account_id / "account.json")
        return {**rec, "id": account_id} if rec else None

    def _rows(self) -> list[dict[str, Any]]:
        rows = []
        if self.root.is_dir():
            for d in self.root.iterdir():
                rec = self.get(d.name) if d.is_dir() else None
                if rec and not rec.get("pending"):
                    rows.append(rec)
        rows.sort(key=lambda r: (r.get("added") or 0, r["id"]))
        return rows

    def list(self) -> list[dict[str, Any]]:
        """Every account, oldest first, each with ``posting`` (the one the publisher uses)."""
        rows = self._rows()
        posting = self.posting_id(rows)
        return [{**r, "posting": r["id"] == posting} for r in rows]

    def posting_id(self, rows: list[dict[str, Any]] | None = None) -> str | None:
        """The account that posts: the one chosen; without a choice the first that is ready, else the
        first that is there."""
        rows = self._rows() if rows is None else rows
        chosen = (_read_json(self.root / "registry.json") or {}).get("posting")
        if any(r["id"] == chosen for r in rows):
            return chosen
        ready = [r for r in rows if r.get("state") == READY]
        return (ready or rows)[0]["id"] if rows else None

    def posting(self) -> dict[str, Any] | None:
        pid = self.posting_id()
        return self.get(pid) if pid else None

    def legacy(self) -> Path | None:
        """The old single sign-in, when it is there and no account uses it yet."""
        p = self.legacy_profile()
        if not p.is_dir() or not any(p.iterdir()):
            return None
        used = {str(Path(r["profile"])) for r in self.list() if r.get("profile")}
        return None if str(p) in used else p

    # -- changing ------------------------------------------------------------------------------------------
    def create(self, *, profile: Path | None = None) -> dict[str, Any]:
        """A new account that nobody has signed in to yet (``pending`` until a channel was read)."""
        with _GUARD:
            account_id = secrets.token_hex(6)
            rec: dict[str, Any] = {"added": self.now(), "state": UNKNOWN, "pending": True}
            if profile is not None:
                rec["profile"] = str(profile)
            _write_json(self.folder(account_id) / "account.json", rec)
            return {**rec, "id": account_id}

    def update(self, account_id: str, **fields: Any) -> dict[str, Any]:
        with _GUARD:
            rec = _read_json(self.folder(account_id) / "account.json")
            if rec is None:
                raise ValueError("No such account.")
            rec.update(fields)
            if rec.get("pending") is False:
                rec.pop("pending")
            _write_json(self.folder(account_id) / "account.json", rec)
            return {**rec, "id": account_id}

    def set_posting(self, account_id: str) -> None:
        with _GUARD:
            if not any(r["id"] == account_id for r in self._rows()):
                raise ValueError("No such account.")
            _write_json(self.root / "registry.json", {"posting": account_id})

    def remove(self, account_id: str) -> None:
        """Forget an account and delete its Chrome profile. An adopted old profile is left where it is."""
        with _GUARD:
            folder = self.folder(account_id)
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)

    def sweep(self, keep: str | None = None) -> None:
        """Delete what an unfinished sign-in left: accounts that never got as far as a channel."""
        if not self.root.is_dir():
            return
        for d in self.root.iterdir():
            if d.name == keep or not ACCOUNT_ID.fullmatch(d.name):
                continue
            rec = _read_json(d / "account.json")
            if rec is None or rec.get("pending"):
                shutil.rmtree(d, ignore_errors=True)

    def found(self, account_id: str, channel: dict[str, Any]) -> dict[str, Any]:
        """Keep what a sign-in or check read: the channel, its picture, that it is ready, and when."""
        fields: dict[str, Any] = {"state": READY, "checked": self.now(), "pending": False}
        for k in ("channel_id", "name", "handle", "subscribers", "picture_url"):
            if channel.get(k) not in (None, ""):
                fields[k] = channel[k]
        picture = self.keep_image(channel.get("picture_url"))
        if picture:
            fields["picture"] = picture
        return self.update(account_id, **fields)

    def same_channel(self, account_id: str, channel_id: str | None) -> dict[str, Any] | None:
        """Another account that is already signed in to this channel."""
        if not channel_id:
            return None
        return next(
            (r for r in self.list() if r["id"] != account_id and r.get("channel_id") == channel_id), None
        )

    # -- pictures ------------------------------------------------------------------------------------------
    def keep_image(self, url: str | None) -> str | None:
        """Download a channel picture once and return the id it is kept under; None when it cannot be had."""
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

    def image(self, image_id: str) -> tuple[Path, str] | None:
        """A kept picture by its id: the file and its content type, read from how the file begins."""
        if not IMAGE_ID.fullmatch(image_id or ""):
            return None
        path = self.root / "images" / image_id
        try:
            with path.open("rb") as f:
                ctype = image_type(f.read(16))
        except OSError:
            return None
        return (path, ctype) if ctype else None

    @staticmethod
    def public(rec: dict[str, Any]) -> dict[str, Any]:
        """An account as the page shows it."""
        out = {k: rec.get(k) for k in _FIELDS}
        out.update(
            id=rec["id"],
            posting=bool(rec.get("posting")),
            picture=f"/connect/image/{rec['picture']}" if rec.get("picture") else None,
            adopted=bool(rec.get("profile")),
        )
        return out


def posting_profile() -> Path | None:
    """The Chrome profile of the account that posts, for the browser publisher; None when no account is
    kept (the publisher then uses the old single profile)."""
    accounts = Accounts()
    rec = accounts.posting()
    return accounts.profile_path(rec["id"]) if rec else None
