"""Live status polling (Twitch Helix, Kick) and the recorder/chat supervisor."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .. import config
from ..queue.db import ClipDB
from .recorder import Recorder

Status = dict[str, dict[str, Any]]
HttpFn = Callable[..., dict]
STALE_SEGMENT_S = 120


def http_json(
    method: str, url: str, headers: dict | None = None, data: dict | None = None, timeout: float = 15
) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def _offline(login: str) -> dict:
    return {"live": False, "stream_id": None, "viewers": 0, "title": "", "game": "", "started_at": None}


class TwitchPoller:
    def __init__(
        self, client_id: str | None = None, client_secret: str | None = None, http: HttpFn = http_json
    ):
        self.cid = client_id or os.environ.get("TWITCH_CLIENT_ID", "")
        self.secret = client_secret or os.environ.get("TWITCH_CLIENT_SECRET", "")
        self.http = http
        self._token: str | None = None
        self._exp = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = self.http(
            "POST",
            "https://id.twitch.tv/oauth2/token",
            None,
            {"client_id": self.cid, "client_secret": self.secret, "grant_type": "client_credentials"},
        )
        self._token, self._exp = r["access_token"], time.time() + float(r.get("expires_in", 3600))
        return self._token

    def poll(self, logins: list[str]) -> Status:
        out: Status = {lg.lower(): _offline(lg) for lg in logins}
        for i in range(0, len(logins), 100):
            chunk = logins[i : i + 100]
            q = urllib.parse.urlencode([("user_login", lg) for lg in chunk] + [("first", 100)])
            r = self.http(
                "GET",
                f"https://api.twitch.tv/helix/streams?{q}",
                {"Client-Id": self.cid, "Authorization": f"Bearer {self.token()}"},
            )
            for s in r.get("data", []):
                if s.get("type", "live") != "live":
                    continue
                out[s["user_login"].lower()] = {
                    "live": True,
                    "stream_id": s["id"],
                    "viewers": s.get("viewer_count", 0),
                    "title": s.get("title", ""),
                    "game": s.get("game_name", ""),
                    "started_at": s.get("started_at"),
                }
        return out


class KickPoller:
    """UNVERIFIED against the live Kick public API: endpoint shape and field names are assumed."""

    def __init__(
        self, client_id: str | None = None, client_secret: str | None = None, http: HttpFn = http_json
    ):
        self.cid = client_id or os.environ.get("KICK_CLIENT_ID", "")
        self.secret = client_secret or os.environ.get("KICK_CLIENT_SECRET", "")
        self.http = http
        self._token: str | None = None
        self._exp = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        r = self.http(
            "POST",
            "https://id.kick.com/oauth/token",
            None,
            {"client_id": self.cid, "client_secret": self.secret, "grant_type": "client_credentials"},
        )
        self._token, self._exp = r["access_token"], time.time() + float(r.get("expires_in", 3600))
        return self._token

    def poll(self, slugs: list[str]) -> Status:
        out: Status = {s.lower(): _offline(s) for s in slugs}
        q = urllib.parse.urlencode([("slug", s) for s in slugs])
        r = self.http(
            "GET", f"https://api.kick.com/public/v1/channels?{q}", {"Authorization": f"Bearer {self.token()}"}
        )
        for ch in r.get("data", []):
            st = ch.get("stream") or {}
            if not st.get("is_live"):
                continue
            slug = str(ch.get("slug", "")).lower()
            out[slug] = {
                "live": True,
                "stream_id": str(st.get("start_time") or ch.get("broadcaster_user_id", slug)),
                "viewers": st.get("viewer_count", 0),
                "title": ch.get("stream_title", ""),
                "game": (ch.get("category") or {}).get("name", ""),
                "started_at": st.get("start_time"),
            }
        return out


def sd_notify(msg: str) -> None:
    """systemd watchdog/ready notification via NOTIFY_SOCKET (no-op elsewhere)."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.sendto(msg.encode(), path)


class Heartbeat:
    """Heartbeat rows in the shared SQLite file."""

    def __init__(self, db_path: str | Path):
        self.db = ClipDB(db_path)
        self.db._conn().execute(
            "CREATE TABLE IF NOT EXISTS heartbeat(name TEXT PRIMARY KEY, ts REAL, detail TEXT)"
        )

    def beat(self, name: str, detail: str = "") -> None:
        self.db._conn().execute("INSERT OR REPLACE INTO heartbeat VALUES(?,?,?)", (name, time.time(), detail))

    def last(self, name: str) -> float | None:
        r = self.db._conn().execute("SELECT ts FROM heartbeat WHERE name=?", (name,)).fetchone()
        return r["ts"] if r else None


class Supervisor:
    """Starts/stops recorder + chat task per streamer from polled status."""

    def __init__(
        self,
        streamers: dict[str, dict],
        ring_root: str | Path,
        heartbeat: Heartbeat | None = None,
        *,
        twitch: TwitchPoller | None = None,
        kick: KickPoller | None = None,
        recorder_factory: Callable[..., Any] = Recorder,
        chat_factory: Callable[[str, str, str], Awaitable[None]] | None = None,
    ):
        self.streamers = {k.lower(): v for k, v in streamers.items()}
        self.ring_root = ring_root
        self.hb = heartbeat
        self.twitch, self.kick = twitch or TwitchPoller(), kick or KickPoller()
        self.recorder_factory, self.chat_factory = recorder_factory, chat_factory
        self.recorders: dict[str, Any] = {}
        self.chat_tasks: dict[str, asyncio.Task] = {}

    def _platform(self, login: str) -> str:
        return self.streamers[login].get("platform", "twitch")

    def poll(self) -> Status:
        res: Status = {}
        for plat, poller in (("twitch", self.twitch), ("kick", self.kick)):
            logins = [lg for lg in self.streamers if self._platform(lg) == plat]
            if logins:
                res.update(poller.poll(logins))
        return res

    async def tick(self, status: Status | None = None, now: float | None = None) -> Status:
        status = status if status is not None else await asyncio.to_thread(self.poll)
        now = time.time() if now is None else now
        for login, st in status.items():
            rec = self.recorders.get(login)
            if st["live"]:
                if rec is None or rec.stream_id != st["stream_id"]:
                    if rec:
                        await self._stop(login)
                    await self._start(login, st)
                elif (age := rec.newest_segment_age_s(now)) is not None and age > STALE_SEGMENT_S:
                    await rec.start()
            elif rec:
                await self._stop(login)
        if self.hb:
            self.hb.beat("watcher", json.dumps({"live": [k for k, v in status.items() if v["live"]]}))
        sd_notify("WATCHDOG=1")
        return status

    async def _start(self, login: str, st: dict) -> None:
        plat = self._platform(login)
        rec = self.recorder_factory(
            plat,
            login,
            st["stream_id"],
            self.ring_root,
            quality=self.streamers[login].get("quality", "720p60,720p,best"),
        )
        self.recorders[login] = rec
        await rec.start()
        if self.chat_factory:
            self.chat_tasks[login] = asyncio.create_task(self.chat_factory(plat, login, st["stream_id"]))

    async def _stop(self, login: str) -> None:
        rec = self.recorders.pop(login, None)
        if rec:
            await rec.stop()
        t = self.chat_tasks.pop(login, None)
        if t:
            t.cancel()

    async def run(self, poll_s: float = 60) -> None:
        sd_notify("READY=1")
        while True:
            try:
                await self.tick()
            except Exception as e:  # keep supervising through API errors
                print(f"watcher tick failed: {e!r}", flush=True)
            await asyncio.sleep(poll_s)


def main() -> None:
    root = Path(os.environ.get("CLIPWATCHER_DATA", config.ROOT / "work"))
    sup = Supervisor(config.streamers(), root / "ring", Heartbeat(root / "queue.db"))
    asyncio.run(sup.run())


if __name__ == "__main__":
    main()
