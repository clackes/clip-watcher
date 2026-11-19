"""Twitch chat replay: everything said in a stream since its first second, also while it is still live.

IRC has no history, so a bot that joins two hours in never saw the first two hours. Twitch does keep the chat
of every archived stream next to the VOD and serves it by video offset, and the VOD of a stream that is live
right now is already being written: the replay reaches back to the start and up to ~15 s behind live. Every
comment carries its real send time, so replayed messages land on the same epoch-ms timeline as IRC.

Anonymous, like the rest of the live lane (the public web client id, no login). Channels with VODs turned off
have no replay: ``live_session`` then returns ``vod_id=None`` and the caller starts from what it has.
Paging is by offset because cursors need a signed-in browser.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from clipwatcher import config
from clipwatcher.models import ChatMessage

log = logging.getLogger(__name__)

GQL_URL = "https://gql.twitch.tv/gql"
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # twitch.tv's own public player id, not a secret
_SESSION_Q = (
    "query($login: String!) { user(login: $login) { id stream { id createdAt archiveVideo { id } } } }"
)
_PAGE_Q = (
    "query($id: ID!, $off: Int) { video(id: $id) { lengthSeconds comments(contentOffsetSeconds: $off) {"
    " edges { node { id contentOffsetSeconds createdAt commenter { login } message { fragments { text } } } }"
    " } } }"
)
_SLOTS = threading.BoundedSemaphore(3)  # nine lanes starting together must not open 72 connections at once
# The replay carries no USERNOTICE tags; raids and gift bombs are recognised by Twitch's own wording.
_RAID = re.compile(r"^(\d+) raiders from \S+ have joined!?\s*$")
_GIFT_BOMB = re.compile(r"^\S+ is gifting (\d+) .*Subs? to \S+ community!", re.IGNORECASE)
_GIFT_ONE = re.compile(r"^\S+ gifted a .*sub to \S+!", re.IGNORECASE)
_SUB = re.compile(r"^\S+ subscribed (with Prime|at Tier \d)\.")

Post = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class Session:
    """One broadcast: the id Twitch gives it, when it started, and its VOD while it is being recorded."""

    stream_id: str
    started_ms: int
    vod_id: str | None
    user_id: str | None = None  # the broadcaster, for Helix (viewer clips)
    platform: str = "twitch"  # kick: vod_id is the channel id, which is what its chat replay goes by


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        GQL_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Client-ID": os.environ.get("CLIPWATCHER_TWITCH_CLIENT_ID", WEB_CLIENT_ID),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - fixed https URL
        return json.load(r)


def _iso_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def live_session(login: str, *, post: Post | None = None) -> Session | None:
    """The broadcast ``login`` is running right now, or None when offline / unknown."""
    d = (post or _post)({"query": _SESSION_Q, "variables": {"login": login.lower()}})
    user = (d.get("data") or {}).get("user") or {}
    stream = user.get("stream")
    if not stream:
        return None
    vod = (stream.get("archiveVideo") or {}).get("id")
    uid = str(user["id"]) if user.get("id") else None
    return Session(str(stream["id"]), _iso_ms(stream["createdAt"]), str(vod) if vod else None, uid)


def node_to_message(node: dict[str, Any], bots: set[str] | frozenset[str] = frozenset()) -> ChatMessage:
    user = ((node.get("commenter") or {}).get("login") or "").lower()
    text = "".join(f.get("text") or "" for f in (node.get("message") or {}).get("fragments") or [])
    ntype, viewers = None, None
    if m := _RAID.match(text):
        ntype, viewers = "raid", int(m.group(1))
    elif m := _GIFT_BOMB.match(text):
        ntype, viewers = "submysterygift", int(m.group(1))
    elif _GIFT_ONE.match(text):
        ntype = "subgift"
    elif _SUB.match(text):
        ntype = "sub"
    return ChatMessage(
        ts_ms=_iso_ms(node["createdAt"]),
        user=user,
        text=text,
        platform="twitch",
        is_bot=user in bots,
        is_notice=ntype is not None,
        notice_type=ntype,
        viewers=viewers,
    )


def _page(vod_id: str, off: int, post: Post, tries: int = 4) -> tuple[list[dict[str, Any]], bool]:
    """(comments around a VOD offset, whether the replay ends here). LookupError when the VOD is gone."""
    for attempt in range(tries):
        try:
            d = post({"query": _PAGE_Q, "variables": {"id": vod_id, "off": int(off)}})
            video = (d.get("data") or {}).get("video")
            if video is None:
                if d.get("errors"):
                    raise RuntimeError(str(d["errors"])[:200])
                raise LookupError(f"no VOD {vod_id}")
            comments = video.get("comments")
            if comments is None:
                # Asked past the end, Twitch answers "service error" (the length trails a live stream a bit).
                if off >= int(video.get("lengthSeconds") or 0) - 60:
                    return [], True
                raise RuntimeError(str(d.get("errors"))[:200])
            return [e["node"] for e in comments.get("edges") or []], False
        except LookupError:
            raise
        except Exception:  # noqa: BLE001 - network hiccup or a Twitch "service error": retry, then give up
            if attempt == tries - 1:
                raise
            time.sleep(0.5 * (attempt + 1))
    return [], True


def _crawl(vod_id: str, lo: int, hi: int, post: Post) -> list[dict[str, Any]]:
    """Every comment with lo <= offset < hi. A page holds ~60 comments and starts a few seconds before the
    offset asked for, so the next page starts at the last offset seen and duplicates are dropped by id."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    off = lo
    while off < hi:
        try:
            nodes, ended = _page(vod_id, off, post)
        except LookupError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("chat replay %s: offset %d failed (%s); skipping 5 s", vod_id, off, e)
            off += 5
            continue
        if ended:
            break  # a stream still live: the replay stops here for now
        for n in nodes:
            if n["id"] not in seen and lo <= n["contentOffsetSeconds"] < hi:
                seen.add(n["id"])
                out.append(n)
        last = nodes[-1]["contentOffsetSeconds"] if nodes else off
        if not nodes:
            off += 30  # nobody typed here
        elif last > off:
            off = last
        else:
            off += 1  # one second busier than a page: take what came and move on
    return out


def fetch(
    vod_id: str,
    start_s: float,
    end_s: float,
    *,
    post: Post | None = None,
    bots: set[str] | None = None,
    workers: int = 6,
) -> list[ChatMessage]:
    """Replayed chat between two VOD offsets (seconds), oldest first, timestamps in epoch ms."""
    lo, hi = max(0, int(start_s)), int(end_s)
    if hi <= lo:
        return []
    if bots is None:
        bots = {b.lower() for b in config.global_config().get("detector", {}).get("bots", [])}
    post = post or _post
    step = max(300, (hi - lo) // (max(1, workers) * 4) + 1)
    ranges = [(a, min(hi, a + step)) for a in range(lo, hi, step)]
    try:
        with _SLOTS:
            if len(ranges) == 1 or workers <= 1:
                parts = [_crawl(vod_id, a, b, post) for a, b in ranges]
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    parts = list(ex.map(lambda r: _crawl(vod_id, r[0], r[1], post), ranges))
    except LookupError:
        log.info("chat replay: VOD %s is gone", vod_id)
        return []
    msgs = [node_to_message(n, bots) for part in parts for n in part]
    msgs.sort(key=lambda m: m.ts_ms)
    return msgs


def fetch_between(
    s: Session, a_ms: float, b_ms: float, budget_s: float | None = None
) -> tuple[list[ChatMessage], int]:
    """StreamSession's history source for Twitch: replayed chat strictly between two instants, and how far
    back the read got (always all the way: the replay is fetched in parallel, no budget needed)."""
    off = lambda ms: (ms - s.started_ms) / 1000  # noqa: E731 - the VOD starts with the stream, give or take
    msgs = fetch(str(s.vod_id), off(a_ms) - 30, off(b_ms) + 30)
    return [m for m in msgs if a_ms < m.ts_ms < b_ms], int(a_ms)
