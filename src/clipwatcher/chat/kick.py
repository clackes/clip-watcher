"""Kick live chat over the Pusher WebSocket (unofficial, UNVERIFIED against the live service).

The Pusher app key and the channel API are not documented by Kick; both can change. ``websockets`` is imported
lazily. The channel lookup may be behind Cloudflare: pass ``headers_provider`` (e.g. returning cookies from a
patchright session) to ``resolve_chatroom_id``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from clipwatcher import config
from clipwatcher.chat.twitch_irc import backoff_delay
from clipwatcher.models import ChatMessage

log = logging.getLogger(__name__)

DEFAULT_KEY = "32cbd69e4b950bf97679"  # unverified, taken from the public web client
CHAT_EVENT = "App\\Events\\ChatMessageEvent"
GIFT_EVENT = "App\\Events\\GiftedSubscriptionsEvent"
_EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")


def pusher_url(key: str | None = None) -> str:
    key = key or config.global_config().get("kick", {}).get("pusher_key") or DEFAULT_KEY
    return f"wss://ws-us2.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"


def strip_emotes(text: str) -> str:
    return _EMOTE.sub(r"\1", text)


def iso_to_ms(s: str) -> int:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def api_message_to_chat(
    d: dict[str, Any], bots: set[str] | frozenset[str] = frozenset()
) -> ChatMessage | None:
    """Kick chat message object (Pusher payload or messages API item) -> ChatMessage."""
    content = d.get("content")
    if content is None:
        return None
    sender = d.get("sender") or {}
    user = str(sender.get("slug") or sender.get("username") or "").lower().replace("-", "_")
    created = d.get("created_at")
    try:
        ts = iso_to_ms(created) if isinstance(created, str) else int(created)
    except (TypeError, ValueError):
        return None
    return ChatMessage(ts_ms=ts, user=user, text=strip_emotes(content), platform="kick", is_bot=user in bots)


def parse_pusher_frame(raw: str, bots: set[str] | frozenset[str] = frozenset()) -> ChatMessage | None:
    try:
        frame = json.loads(raw)
        data = frame.get("data")
        if isinstance(data, str):
            data = json.loads(data)
    except (ValueError, AttributeError):
        return None
    ev = frame.get("event")
    if ev == CHAT_EVENT and isinstance(data, dict):
        return api_message_to_chat(data, bots)
    if ev == GIFT_EVENT and isinstance(data, dict):
        n = len(data.get("gifted_usernames") or []) or None
        return ChatMessage(
            ts_ms=int(datetime.now(UTC).timestamp() * 1000),
            user=str(data.get("gifter_username", "")).lower(),
            text="",
            platform="kick",
            is_notice=True,
            notice_type="submysterygift",
            viewers=n,
        )
    return None


def _http_json(url: str, headers: dict[str, str] | None = None, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https endpoints
        return json.loads(r.read().decode("utf-8"))


def resolve_chatroom_id(
    slug: str,
    *,
    fetch: Callable[[str, dict[str, str]], Any] | None = None,
    headers_provider: Callable[[], dict[str, str]] | None = None,
) -> int:
    """Unofficial /api/v2/channels/{slug}. Hook: ``headers_provider`` returns Cloudflare cookies."""
    headers = headers_provider() if headers_provider else {}
    data = (fetch or (lambda u, h: _http_json(u, h)))(f"https://kick.com/api/v2/channels/{slug}", headers)
    return int(data["chatroom"]["id"])


class KickChat:
    """``async for msg in KickChat(slug, chatroom_id=...)``. ``connect(url)`` is an injectable factory that
    returns an async context manager yielding a websocket (send / recv); default uses ``websockets``."""

    def __init__(
        self,
        slug: str,
        *,
        chatroom_id: int | None = None,
        key: str | None = None,
        connect: Callable[[str], Any] | None = None,
        headers_provider: Callable[[], dict[str, str]] | None = None,
        bots: set[str] | None = None,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        idle_timeout: float = 150.0,
    ):
        self.slug = slug.lower()
        self.chatroom_id = chatroom_id
        self.url = pusher_url(key)
        self._connect = connect
        self._headers = headers_provider
        if bots is None:
            bots = {b.lower() for b in config.global_config().get("detector", {}).get("bots", [])}
        self.bots = bots
        self.backoff_base, self.backoff_cap, self.idle_timeout = backoff_base, backoff_cap, idle_timeout
        self.connects = 0
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

    async def messages(self) -> AsyncIterator[ChatMessage]:
        attempt = 0
        while not self._closed:
            try:
                if self.chatroom_id is None:
                    self.chatroom_id = await asyncio.to_thread(
                        resolve_chatroom_id, self.slug, headers_provider=self._headers
                    )
                async with self._factory()(self.url) as ws:
                    self.connects += 1
                    sub = {
                        "event": "pusher:subscribe",
                        "data": {"auth": "", "channel": f"chatrooms.{self.chatroom_id}.v2"},
                    }
                    await ws.send(json.dumps(sub))
                    while not self._closed:
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.idle_timeout)
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "replace")
                        if '"pusher:ping"' in raw:
                            await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                            continue
                        msg = parse_pusher_frame(raw, self.bots)
                        if msg:
                            attempt = 0
                            yield msg
            except (TimeoutError, OSError, ConnectionError, EOFError):
                pass
            except Exception as e:  # noqa: BLE001 - websockets raises its own ConnectionClosed family
                if type(e).__module__.split(".")[0] != "websockets":
                    raise
            if self._closed:
                return
            await asyncio.sleep(backoff_delay(attempt, self.backoff_base, self.backoff_cap))
            attempt += 1


# --------------------------------------------------------------------------- history polling (default)
HISTORY_URL = "https://web.kick.com/api/v1/chat/{channel_id}/history"


def resolve_channel_id(slug: str, *, fetch: Callable[[str, dict[str, str]], Any] | None = None) -> int:
    data = (fetch or (lambda u, h: _http_json(u, h)))(f"https://kick.com/api/v2/channels/{slug}", {})
    return int(data["id"])


def history_message_to_chat(m: dict, bots: set[str] | frozenset[str] = frozenset()) -> ChatMessage | None:
    """web.kick.com chat history item -> ChatMessage. ``metadata.message_ref`` carries epoch ms."""
    if m.get("type") not in (None, "message", "reply"):
        return None
    ts = None
    try:
        meta = m.get("metadata")
        meta = json.loads(meta) if isinstance(meta, str) else meta
        ts = int((meta or {}).get("message_ref"))
    except (TypeError, ValueError, AttributeError):
        pass
    if not ts:
        ts = iso_to_ms(m["created_at"])
    user = (m.get("sender") or {}).get("slug") or (m.get("sender") or {}).get("username", "")
    user = user.lower()
    return ChatMessage(ts, user, strip_emotes(m.get("content", "")), platform="kick", is_bot=user in bots)


class KickHistoryChat:
    """``async for msg in KickHistoryChat(slug)``: polls the public chat history (latest 25 messages) every
    ``poll_s`` and yields unseen messages in time order. Kick's web client moved off Pusher in 2026; this
    needs no login or websocket. A poll with no overlap with the previous one means messages were missed
    (only on very fast chats) and is counted in ``self.gaps``."""

    def __init__(self, slug: str, *, channel_id: int | None = None, poll_s: float = 1.0,
                 fetch: Callable[[str, dict[str, str]], Any] | None = None,
                 sleep: Callable[[float], Any] = asyncio.sleep, bots: set[str] | None = None):  # fmt: skip
        self.slug, self.channel_id, self.poll_s = slug, channel_id, poll_s
        self.fetch = fetch or (lambda u, h: _http_json(u, h))
        self.sleep = sleep
        self.bots = (
            bots if bots is not None else set(config.global_config().get("detector", {}).get("bots", []))
        )
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
        if self._closed:
            return
        url = HISTORY_URL.format(channel_id=self.channel_id)
        seen: dict[str, None] = {}
        prev: set[str] = set()
        attempt = 0
        first = self.skip_backlog
        while not self._closed:
            try:
                data = await asyncio.to_thread(self.fetch, url, {"Accept": "application/json"})
                items = (data.get("data") or {}).get("messages") or []
                attempt = 0
            except Exception:  # noqa: BLE001 - network hiccup: back off and keep polling
                attempt += 1
                await self.sleep(backoff_delay(attempt))
                continue
            ids = {m["id"] for m in items}
            if first:  # the first page is backlog from before we started: remember it, don't replay it
                first = False
                seen.update(dict.fromkeys(ids))
                prev = ids
                await self.sleep(self.poll_s)
                continue
            if prev and ids and not (ids & prev):
                self.gaps += 1
                if self.gaps in (1, 10, 100) or self.gaps % 1000 == 0:  # say so: the spike counts run low
                    log.warning(
                        "kick %s: chat is faster than the history endpoint returns; messages were missed "
                        "in %d polls so far",
                        self.slug,
                        self.gaps,
                    )
            prev = ids
            out = []
            for m in items:
                if m["id"] in seen:
                    continue
                seen[m["id"]] = None
                try:
                    msg = history_message_to_chat(m, self.bots)
                except Exception:  # noqa: BLE001 - one malformed message must not stop the lane
                    msg = None
                if msg is not None:
                    out.append(msg)
            while len(seen) > 2000:
                seen.pop(next(iter(seen)))
            for msg in sorted(out, key=lambda x: x.ts_ms):
                yield msg
            await self.sleep(self.poll_s)
