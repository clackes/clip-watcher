"""Kick chat: live, replayed and polled. Anonymous (no login, no API key); checked against the live service
on 19 Nov 2026.

Live: Kick's web client asks ``POST web.kick.com/api/v1/realtime/channels/<channel id>/chat/connection`` which
realtime server to use and gets a Centrifugo websocket (Pusher is gone: its old app key is refused). A guest
token comes from ``POST .../realtime/auth/connection``; messages arrive on ``chatrooms.<chatroom id>.v2`` as
``App\\Events\\ChatMessageEvent`` about half a second after they are sent, every one of them.

History: ``GET kick.com/api/v2/channels/<channel id>/messages?cursor=<epoch microseconds>`` returns the 25
messages before that instant and the cursor of the page before those. Any instant works as a cursor, on a
stream that is live and on one weeks old. Kick refuses an address that asks too much (429 "Request blocked
by security policy", all of kick.com and web.kick.com together): 5 requests a second pass for ten seconds,
but over a minute only about 1.6 a second do, so every request in this module shares one limiter set below
that, and history comes in at about 35 messages a second however it is asked for. A block lifts within 5 s.

Time: ``created_at`` is the server's clock, in whole seconds. ``metadata.message_ref`` is the *viewer's*
clock: 6% of messages were more than 5 s off, some a full hour, some 40 s ahead. A stamp from the future
moves the detector's clock forward and makes it drop the real chat that follows, so ``message_ref`` only
orders messages inside their second.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from clipwatcher import config
from clipwatcher.chat.twitch_irc import backoff_delay
from clipwatcher.chat.twitch_replay import Session
from clipwatcher.models import ChatMessage

log = logging.getLogger(__name__)

API = "https://kick.com/api/v2"
REALTIME = "https://web.kick.com/api/v1/realtime"
PAGE = 25  # messages per history page
_EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")
_EVENT_PREFIX = "App\\Events\\"
_WEB = {"Origin": "https://kick.com", "Referer": "https://kick.com/", "Content-Type": "application/json"}
REF_SLACK_MS = 1500  # message_ref is believed only this close to created_at

Fetch = Callable[[str, dict[str, str]], Any]
Post = Callable[[str, dict[str, Any]], Any]


# ----------------------------------------------------------------------------------------- HTTP
class _Limiter:
    """Evenly spaced requests, shared by every thread: at most ``rps`` a second, and nobody asks at all
    while Kick is refusing us."""

    def __init__(self, rps: float):
        self.gap = 1.0 / max(0.1, rps)
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + self.gap
        if at > now:
            time.sleep(at - now)

    def blocked(self, pause_s: float = 6.0) -> None:
        """Kick answered 429: every caller holds off until the block has lifted (it does within 5 s)."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + pause_s)


_LIMITER: _Limiter | None = None


def _limiter() -> _Limiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = _Limiter(float(config.global_config().get("kick", {}).get("max_rps", 1.5)))
    return _LIMITER


def _http_json(
    url: str, headers: dict[str, str] | None = None, timeout: float = 15.0, body: dict[str, Any] | None = None
) -> Any:
    """One request to Kick, paced by the shared limiter. A 429 (blocked for asking too much) pauses every
    caller and is retried for over a minute; anything else is the caller's to handle."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", **(_WEB if body is not None else {})}
    tries = 12
    for attempt in range(tries):
        _limiter().wait()
        req = urllib.request.Request(url, data=data, headers={**hdrs, **(headers or {})})  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https endpoints
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:
                raise
            _limiter().blocked()
    raise RuntimeError("unreachable")


def _post_json(url: str, body: dict[str, Any]) -> Any:
    return _http_json(url, None, 15.0, body)


# ------------------------------------------------------------------------------------- channel
@dataclass
class Channel:
    id: int  # what the chat history is addressed by
    chatroom_id: int  # what the realtime channels are named after
    slug: str
    stream_id: str | None = None  # the broadcast running right now
    started_ms: int | None = None
    category: str | None = None  # what it shows, as Kick names it ("Grand Theft Auto VI (GTA)")


def iso_to_ms(s: str) -> int:
    """ISO time ("...Z", "+00:00") or Kick's "YYYY-MM-DD HH:MM:SS" (UTC) -> epoch ms."""
    dt = datetime.fromisoformat(s.strip().replace(" ", "T").replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def channel(slug: str, *, fetch: Fetch | None = None) -> Channel:
    d = (fetch or _http_json)(f"{API}/channels/{slug.lower()}", {})
    ls = d.get("livestream") or {}
    live = bool(ls) and ls.get("is_live", True)
    start = ls.get("start_time") or ls.get("created_at")
    return Channel(
        int(d["id"]),
        int((d.get("chatroom") or {})["id"]),
        str(d.get("slug") or slug).lower(),
        str(ls["id"]) if live and ls.get("id") else None,
        iso_to_ms(start) if live and start else None,
        ((ls.get("categories") or [{}])[0] or {}).get("name") if live else None,
    )


def resolve_channel_id(slug: str, *, fetch: Fetch | None = None) -> int:
    return channel(slug, fetch=fetch).id


def live_session(slug: str, *, fetch: Fetch | None = None) -> Session | None:
    """The broadcast ``slug`` is running right now, or None when offline. ``vod_id`` is the channel id:
    that is what Kick's chat replay is addressed by."""
    ch = channel(slug, fetch=fetch)
    if not (ch.stream_id and ch.started_ms):
        return None
    return Session(ch.stream_id, ch.started_ms, str(ch.id), str(ch.id), "kick", ch.category)


# ------------------------------------------------------------------------------------ messages
def strip_emotes(text: str) -> str:
    return _EMOTE.sub(r"\1", text)


def created_ms(d: dict[str, Any]) -> int | None:
    c = d.get("created_at")
    try:
        return iso_to_ms(c) if isinstance(c, str) else int(c)
    except (TypeError, ValueError):
        return None


def message_to_chat(d: dict[str, Any], bots: set[str] | frozenset[str] = frozenset()) -> ChatMessage | None:
    """A Kick chat message (realtime payload, history item) -> ChatMessage, timed by the server."""
    if d.get("type") not in (None, "message", "reply") or d.get("content") is None:
        return None
    ts = created_ms(d)
    if ts is None:
        return None
    try:
        meta = d.get("metadata")
        ref = int((json.loads(meta) if isinstance(meta, str) else meta or {}).get("message_ref"))
        if abs(ref - ts) <= REF_SLACK_MS:  # the viewer's clock agrees with the server's: use its milliseconds
            ts = ref
    except (TypeError, ValueError, AttributeError):
        pass
    sender = d.get("sender") or {}
    user = str(sender.get("slug") or sender.get("username") or "").lower()
    return ChatMessage(ts, user, strip_emotes(str(d["content"])), platform="kick", is_bot=user in bots)


api_message_to_chat = history_message_to_chat = message_to_chat  # earlier names of the same thing


def _notice(kind: str, user: Any, viewers: Any, now_ms: int) -> ChatMessage:
    try:
        n = int(viewers) if viewers is not None else None
    except (TypeError, ValueError):
        n = None
    return ChatMessage(
        now_ms, str(user or "").lower(), "", platform="kick", is_notice=True, notice_type=kind, viewers=n
    )


def event_to_chat(
    event: str, data: Any, bots: set[str] | frozenset[str] = frozenset(), now_ms: int | None = None
) -> tuple[str | None, ChatMessage] | None:
    """One realtime event -> (message id or None, ChatMessage). Chat messages, and the three events the
    detector mutes around like on Twitch: gifted subs (a bomb of 5+), a host arriving (a raid), a sub."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    name = event.removeprefix(_EVENT_PREFIX)
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if name == "ChatMessageEvent":
        m = message_to_chat(data, bots)
        return (str(data.get("id")) if data.get("id") else None, m) if m else None
    if name == "GiftedSubscriptionsEvent":
        if (data.get("chunk_details") or {}).get(
            "chunk_index", 0
        ):  # a big gift comes in chunks: count it once
            return None
        n = data.get("gifted_total") or len(data.get("gifted_usernames") or []) or None
        return None, _notice("submysterygift", data.get("gifter_username"), n, now_ms)
    if name == "StreamHostedEvent":
        user = (data.get("user") or {}).get("username") or data.get("host_username")
        n = (data.get("message") or {}).get("numberOfViewers", data.get("number_viewers"))
        return None, _notice("raid", user, n, now_ms)
    if name == "SubscriptionEvent":
        return None, _notice("sub", data.get("username"), None, now_ms)
    return None


def parse_frame(
    raw: str | bytes, bots: set[str] | frozenset[str] = frozenset()
) -> list[tuple[str | None, ChatMessage]]:
    """Chat carried by one websocket frame (Centrifugo sends one JSON object per line)."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    out = []
    for line in raw.split("\n"):
        try:
            pub = (
                ((json.loads(line).get("push") or {}).get("pub") or {}).get("data") if line.strip() else None
            )
        except (ValueError, AttributeError):
            continue
        if isinstance(pub, dict) and pub.get("event"):
            got = event_to_chat(str(pub["event"]), pub.get("data"), bots)
            if got:
                out.append(got)
    return out


# -------------------------------------------------------------------------------------- replay
def fetch_page(
    channel_id: int | str, cursor_us: int, *, fetch: Fetch | None = None
) -> tuple[list[dict[str, Any]], int | None]:
    """The 25 messages before ``cursor_us`` (epoch microseconds) and the cursor of the page before them."""
    d = (fetch or _http_json)(f"{API}/channels/{channel_id}/messages?cursor={int(cursor_us)}", {})
    data = d.get("data") or {}
    nxt = data.get("cursor")
    return data.get("messages") or [], int(nxt) if nxt else None


def newest_cursor(now_s: float | None = None) -> int:
    """A cursor just ahead of now: the newest page, and an address no cache has seen."""
    return int(((time.time() if now_s is None else now_s) + 5.0) * 1e6)


def _read_back(
    channel_id: int | str, lo_ms: float, hi_ms: float, deadline: float | None, fetch: Fetch | None, clock: Any
) -> tuple[dict[str, dict[str, Any]], int, bool]:
    """Pages backwards from hi_ms to lo_ms: (messages by id, instant reached, whether lo_ms was reached)."""
    out: dict[str, dict[str, Any]] = {}
    # created_at is whole seconds: everything said during hi_ms's second lies before the start of the next
    cur, reached = (int(hi_ms) // 1000 + 1) * 1_000_000, int(hi_ms)
    while deadline is None or clock() < deadline:
        raw, nxt = fetch_page(channel_id, cur, fetch=fetch)
        times = [t for t in (created_ms(d) for d in raw) if t is not None]
        if not times:
            return out, int(lo_ms), True  # nothing was said before this
        for d in raw:
            t = created_ms(d)
            if t is not None and lo_ms <= t <= hi_ms and d.get("id"):
                out[str(d["id"])] = d
        reached = min(times)
        if reached <= lo_ms or nxt is None or nxt >= cur:
            return out, int(lo_ms), True
        cur = nxt
    return out, reached, False


def replay_raw(
    channel_id: int | str,
    start_ms: float,
    end_ms: float,
    *,
    budget_s: float | None = None,
    fetch: Fetch | None = None,
    clock: Callable[[], float] = time.monotonic,
    workers: int = 2,
    slice_s: float = 300.0,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Raw messages by id with start_ms <= created_at <= end_ms, and the instant the read got back to:
    start_ms when everything was read, later when ``budget_s`` ran out first (a long busy stream takes
    minutes at Kick's pace); the caller then comes back for the rest.

    A page only says where the page before it starts, so one reader waits out every answer before it can
    ask again. Any instant is a valid cursor, though: the stretch is cut into 5-minute slices, newest first,
    read by ``workers`` readers at once, so a slow connection does not halve the pace (the limiter keeps the
    total under Kick's block). What comes back is always one unbroken stretch ending at end_ms; a slice read
    out of order is dropped when the budget ends."""
    deadline = clock() + budget_s if budget_s is not None else None
    step = slice_s * 1000
    slices, hi = [], float(end_ms)
    while hi > start_ms:
        lo = max(float(start_ms), hi - step)
        slices.append((lo, hi))
        hi = lo - 1  # created_at is whole seconds: the next slice ends just before this one starts
    if not slices:
        return {}, int(start_ms)
    if len(slices) == 1 or workers <= 1:
        parts = []
        for lo, hi in slices:
            if deadline is not None and clock() >= deadline:
                parts.append(({}, int(hi), False))
            else:
                parts.append(_read_back(channel_id, lo, hi, deadline, fetch, clock))
    else:
        from concurrent.futures import ThreadPoolExecutor

        def one(sl: tuple[float, float]) -> tuple[dict[str, dict[str, Any]], int, bool]:
            if deadline is not None and clock() >= deadline:
                return {}, int(sl[1]), False
            return _read_back(channel_id, sl[0], sl[1], deadline, fetch, clock)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            parts = list(ex.map(one, slices))
    out: dict[str, dict[str, Any]] = {}
    for (lo, _hi), (msgs, reached, done) in zip(slices, parts, strict=True):
        if done:
            out.update(msgs)
            continue
        out.update({i: d for i, d in msgs.items() if (created_ms(d) or 0) >= reached})
        return out, max(int(reached), int(lo))
    return out, int(start_ms)


def in_order(raw: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """Raw (id, message) pairs as fetched (newest first, the way Kick pages them) -> oldest first. Kick only
    stamps whole seconds, so inside a second the order of arrival is the order that is kept."""

    def at(pair: tuple[str, dict[str, Any]]) -> int:
        m = message_to_chat(pair[1])
        return m.ts_ms if m else created_ms(pair[1]) or 0

    return sorted(reversed(raw), key=at)


def replay(
    channel_id: int | str,
    start_ms: float,
    end_ms: float,
    *,
    budget_s: float | None = None,
    bots: set[str] | None = None,
    fetch: Fetch | None = None,
    clock: Callable[[], float] = time.monotonic,
    workers: int = 2,
) -> tuple[list[ChatMessage], int]:
    """Chat between two instants (epoch ms), oldest first, and how far back the read got (see replay_raw)."""
    raw, covered = replay_raw(
        channel_id, start_ms, end_ms, budget_s=budget_s, fetch=fetch, clock=clock, workers=workers
    )
    bots = _bots() if bots is None else bots
    return [m for m in (message_to_chat(d, bots) for _, d in in_order(list(raw.items()))) if m], covered


def fetch_between(
    s: Session, a_ms: float, b_ms: float, budget_s: float | None = None
) -> tuple[list[ChatMessage], int]:
    """StreamSession's history source for Kick: chat of the broadcast between two instants."""
    return replay(str(s.vod_id), a_ms, b_ms, budget_s=budget_s)


def _bots() -> set[str]:
    return {b.lower() for b in config.global_config().get("detector", {}).get("bots", [])}


class _Seen:
    """Ids of the last few thousand messages: the same message can arrive by websocket and by replay."""

    def __init__(self, size: int = 6000):
        self.size, self._ids = size, {}

    def fresh(self, msg_id: str | None) -> bool:
        if msg_id is None:
            return True
        if msg_id in self._ids:
            return False
        self._ids[msg_id] = None
        while len(self._ids) > self.size:
            self._ids.pop(next(iter(self._ids)))
        return True

    def __contains__(self, msg_id: str) -> bool:
        return msg_id in self._ids


def poll_new(
    channel_id: int | str,
    seen: _Seen,
    *,
    fetch: Fetch | None = None,
    max_pages: int = 40,
    since_ms: float = 0.0,
) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """Messages not seen yet, oldest first, and whether the read joined up with what was seen before.
    The newest page is 25 messages; when none of them is known the chat outran one page, and the pages before
    it are followed back until one is, or back to ``since_ms``, or max_pages (about 1000 messages).

    The newest page is asked for with a cursor a few seconds in the future: the address without one is
    cached by Kick's CDN for 10 s, so a poller reading it sees chat up to 10 s late."""
    raw, nxt = fetch_page(channel_id, newest_cursor(), fetch=fetch)
    new = [(str(m["id"]), m) for m in raw if m.get("id") and str(m["id"]) not in seen]
    joined = len(new) < len(raw) or not raw
    pages = 1
    while not joined and nxt and pages < max_pages:
        if min((created_ms(m) or 0) for m in raw) < since_ms:
            joined = True  # back to where the caller's own knowledge starts
            break
        raw, nxt = fetch_page(channel_id, int(nxt), fetch=fetch)
        pages += 1
        fresh = [(str(m["id"]), m) for m in raw if m.get("id") and str(m["id"]) not in seen]
        new += fresh
        joined = len(fresh) < len(raw) or not raw
    return in_order([im for im in new if (created_ms(im[1]) or 0) >= since_ms]), joined


# ---------------------------------------------------------------------------------- live chat
class KickChat:
    """``async for msg in KickChat(slug)``: the live chat of a Kick channel, complete.

    Runs on the realtime websocket. After a dropped connection the stretch that was missed is read from the
    history before new messages are passed on, so a reconnect loses nothing (up to ``gap_max_s``). If the
    websocket cannot be opened ``ws_fail_max`` times in a row the history is polled for ``poll_s_total``
    instead, then the websocket is tried again.

    ``connect(url)`` returns an async context manager yielding a websocket (send / recv); ``fetch(url,
    headers)`` and ``post(url, body)`` are the HTTP calls. All three are injectable for tests.
    """

    def __init__(
        self,
        slug: str,
        *,
        channel_id: int | None = None,
        chatroom_id: int | None = None,
        connect: Callable[[str], Any] | None = None,
        fetch: Fetch | None = None,
        post: Post | None = None,
        bots: set[str] | None = None,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        gap_max_s: float = 900.0,
        ws_fail_max: int = 3,
        poll_s_total: float = 300.0,
        poll_s: float = 1.5,
    ):
        self.slug = slug.lower()
        self.channel_id, self.chatroom_id = channel_id, chatroom_id
        self._connect, self._fetch, self._post = connect, fetch, post or _post_json
        self.bots = _bots() if bots is None else bots
        self.backoff_base, self.backoff_cap = backoff_base, backoff_cap
        self.sleep, self.clock = sleep, clock
        self.gap_max_s, self.ws_fail_max = gap_max_s, ws_fail_max
        self.poll_s_total, self.poll_s = poll_s_total, poll_s
        self.connects = 0  # websocket sessions that got as far as subscribing
        self.filled = 0  # messages recovered from the history after a reconnect
        self.polled = 0  # times the polling fallback took over
        self._seen = _Seen()
        self._last_ms = 0  # server time of the newest chat message passed on
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __aiter__(self) -> AsyncIterator[ChatMessage]:
        return self.messages()

    def _factory(self) -> Callable[[str], Any]:
        if self._connect:
            return self._connect
        import importlib

        return importlib.import_module("websockets").connect

    def _pass(self, msg_id: str | None, m: ChatMessage) -> bool:
        """True when this message has not been passed on yet; remembers it."""
        if not self._seen.fresh(msg_id):
            return False
        if not m.is_notice:
            self._last_ms = max(self._last_ms, m.ts_ms)
        return True

    async def messages(self) -> AsyncIterator[ChatMessage]:
        attempt = fails = 0
        while not self._closed:
            try:
                if self.channel_id is None or self.chatroom_id is None:
                    ch = await asyncio.to_thread(channel, self.slug, fetch=self._fetch)
                    self.channel_id, self.chatroom_id = ch.id, ch.chatroom_id
                async for m in self._session():
                    attempt = fails = 0
                    yield m
                    if self._closed:
                        return
            except (TimeoutError, OSError, ConnectionError, EOFError, ValueError, KeyError, TypeError) as e:
                log.warning("kick %s: realtime connection lost (%s: %s)", self.slug, type(e).__name__, e)
            except Exception as e:  # noqa: BLE001 - websockets raises its own ConnectionClosed family
                if type(e).__module__.split(".")[0] != "websockets":
                    raise
                log.warning("kick %s: realtime connection closed (%s)", self.slug, e)
            if self._closed:
                return
            fails += 1
            if fails >= self.ws_fail_max and self.channel_id is not None:
                log.warning("kick %s: no realtime connection; polling the chat history instead", self.slug)
                self.polled += 1
                async for m in self._poll(self.poll_s_total):
                    yield m
                    if self._closed:
                        return
                fails = self.ws_fail_max - 1  # one try at the websocket; if it is still down, straight back
                continue
            await self.sleep(backoff_delay(attempt, self.backoff_base, self.backoff_cap))
            attempt += 1

    # -- websocket ---------------------------------------------------------------------------
    async def _session(self) -> AsyncIterator[ChatMessage]:
        client_id = str(uuid.uuid4())
        ask = {
            "client": {"id": client_id, "type": "web"},
            "capabilities": {"accepted_providers": [{"provider": "centrifugo"}]},
        }
        conn = await asyncio.to_thread(
            self._post, f"{REALTIME}/channels/{self.channel_id}/chat/connection", ask
        )
        urls = [
            c["credentials"]["url"]
            for c in (conn.get("data") or {}).get("connections") or []
            if c.get("provider") == "centrifugo" and (c.get("credentials") or {}).get("url")
        ]
        if not urls:
            raise ValueError(f"no realtime server offered: {str(conn)[:200]}")
        token = await asyncio.to_thread(self._token, client_id)
        room, idle = self.chatroom_id, 60.0
        # chat lives on the first; gifted subs and hosts are published on the others
        channels = [
            f"chatrooms.{room}.v2",
            f"chatroom_{room}",
            f"chatrooms.{room}",
            f"channel_{self.channel_id}",
        ]
        async with self._factory()(urls[0]) as ws:
            await ws.send(json.dumps({"id": 1, "connect": {"token": token, "name": "js"}}))
            for i, ch in enumerate(channels, 2):
                await ws.send(json.dumps({"id": i, "subscribe": {"channel": ch}}))
            refresh_at: float | None = None
            subscribed = False
            gap: asyncio.Task | None = None
            held: list[tuple[str | None, ChatMessage]] = []
            last_rx = self.clock()
            while not self._closed:
                if gap is not None and gap.done():
                    filled = [
                        (i, message_to_chat(d, self.bots)) for i, d in in_order(list(gap.result()[0].items()))
                    ]
                    gap = None
                    for i, m in filled:
                        if m and self._pass(i, m):
                            self.filled += 1
                            yield m
                    for i, m in held:
                        if self._pass(i, m):
                            yield m
                    held = []
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0 if gap is not None else idle)
                except TimeoutError:
                    if self.clock() - last_rx > idle:
                        raise
                    continue
                last_rx = self.clock()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                for line in raw.split("\n"):
                    if not line.strip():
                        continue
                    frame = json.loads(line)
                    if frame == {}:  # the server's ping
                        await ws.send("{}")
                        continue
                    if "id" in frame:
                        if frame.get("error") and frame["id"] in (
                            1,
                            2,
                        ):  # connect, or the chat channel itself
                            raise ConnectionError(f"realtime refused: {frame['error']}")
                        if frame["id"] == 1 and "connect" in frame:
                            c = frame["connect"]
                            idle = 2.0 * float(c.get("ping") or 25) + 10.0
                            if c.get("expires") and c.get("ttl"):
                                refresh_at = self.clock() + max(30.0, float(c["ttl"]) - 120.0)
                        if frame["id"] == 2 and "subscribe" in frame:
                            subscribed = True
                            self.connects += 1
                            if self._last_ms:  # a reconnect: read what was said while we were away
                                gap = asyncio.create_task(asyncio.to_thread(self._gap, self._last_ms))
                        if "refresh" in frame and (frame["refresh"] or {}).get("ttl"):
                            refresh_at = self.clock() + max(30.0, float(frame["refresh"]["ttl"]) - 120.0)
                        continue
                    if (frame.get("push") or {}).get("disconnect"):
                        raise ConnectionError(f"realtime disconnect: {frame['push']['disconnect']}")
                    for i, m in parse_frame(line, self.bots):
                        if gap is not None:
                            held.append((i, m))
                        elif self._pass(i, m):
                            yield m
                if subscribed and refresh_at is not None and self.clock() >= refresh_at:
                    refresh_at = None  # the guest token lasts 45 minutes: renew it on the open connection
                    token = await asyncio.to_thread(self._token, client_id)
                    await ws.send(json.dumps({"id": 100, "refresh": {"token": token}}))

    def _token(self, client_id: str) -> str:
        d = self._post(f"{REALTIME}/auth/connection", {"client_id": client_id})
        return str(d["data"]["token"])

    def _gap(self, since_ms: int) -> tuple[dict[str, dict[str, Any]], int]:
        now_ms = self.clock() * 1000
        lo = max(since_ms - 2000, now_ms - self.gap_max_s * 1000)
        return replay_raw(self.channel_id, lo, now_ms + 5000, fetch=self._fetch)

    # -- polling fallback --------------------------------------------------------------------
    async def _poll(self, for_s: float) -> AsyncIterator[ChatMessage]:
        until, attempt = self.clock() + for_s, 0
        while not self._closed and self.clock() < until:
            try:
                # Not further back than what this client was there for: with nothing seen yet that is now
                # (what came before is history, the session reads it), after a drop it is the last message.
                since = self._last_ms - 2000 if self._last_ms else self.clock() * 1000 - 5000
                since = max(since, self.clock() * 1000 - self.gap_max_s * 1000)
                new, _ = await asyncio.to_thread(
                    poll_new, self.channel_id, self._seen, fetch=self._fetch, since_ms=since
                )
                if not self._last_ms:
                    self._last_ms = int(self.clock() * 1000)
                attempt = 0
            except Exception as e:  # noqa: BLE001 - network hiccup: back off and keep polling
                attempt += 1
                log.warning("kick %s: history poll failed (%s)", self.slug, e)
                await self.sleep(backoff_delay(attempt, self.backoff_base, self.backoff_cap))
                continue
            for i, d in new:
                m = message_to_chat(d, self.bots)
                if m and self._pass(i, m):
                    yield m
            await self.sleep(self.poll_s)


class KickHistoryChat:
    """``async for msg in KickHistoryChat(slug)``: the chat by polling its history, without a websocket.
    What KickChat falls back to; kept as a source of its own for networks that block websockets.

    Each poll reads the newest page (25 messages). When none of them is known the chat outran one page: the
    pages before it are followed back until one is, so a fast chat is still complete. ``gaps`` counts the
    polls where even that did not join up (more than ~1000 messages between two polls, or no cursor)."""

    def __init__(self, slug: str, *, channel_id: int | None = None, poll_s: float = 1.5,
                 fetch: Fetch | None = None,
                 sleep: Callable[[float], Any] = asyncio.sleep, bots: set[str] | None = None):  # fmt: skip
        self.slug, self.channel_id, self.poll_s = slug, channel_id, poll_s
        self.fetch = fetch or _http_json
        self.sleep = sleep
        self.bots = _bots() if bots is None else bots
        self.gaps = 0
        self.skip_backlog = True
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __aiter__(self) -> AsyncIterator[ChatMessage]:
        return self._run()

    async def _run(self) -> AsyncIterator[ChatMessage]:
        attempt = 0
        while self.channel_id is None and not self._closed:
            # Inside the retry: a failed lookup (Cloudflare, a network blip) used to crash the whole lane,
            # recorder included, and the supervisor restarted it in a loop.
            try:
                self.channel_id = await asyncio.to_thread(resolve_channel_id, self.slug, fetch=self.fetch)
            except Exception as e:  # noqa: BLE001
                attempt += 1
                log.warning("kick %s: channel lookup failed (%s); retrying", self.slug, e)
                await self.sleep(backoff_delay(attempt))
        seen = _Seen()
        attempt = 0
        first = self.skip_backlog
        while not self._closed:
            try:
                if first:  # the newest page is backlog from before we started: remember it, don't replay it
                    raw, _ = await asyncio.to_thread(
                        fetch_page, self.channel_id, newest_cursor(), fetch=self.fetch
                    )
                    for m in raw:
                        seen.fresh(str(m.get("id")))
                    first, new, joined = False, [], True
                else:
                    new, joined = await asyncio.to_thread(poll_new, self.channel_id, seen, fetch=self.fetch)
                attempt = 0
            except Exception:  # noqa: BLE001 - network hiccup: back off and keep polling
                attempt += 1
                await self.sleep(backoff_delay(attempt))
                continue
            if not joined:
                self.gaps += 1
                if self.gaps in (1, 10, 100) or self.gaps % 1000 == 0:  # say so: the spike counts run low
                    log.warning(
                        "kick %s: chat outran the history polling in %d polls so far", self.slug, self.gaps
                    )
            for i, d in new:
                seen.fresh(i)
                try:
                    msg = message_to_chat(d, self.bots)
                except Exception:  # noqa: BLE001 - one malformed message must not stop the lane
                    msg = None
                if msg is not None:
                    yield msg
            await self.sleep(self.poll_s)
