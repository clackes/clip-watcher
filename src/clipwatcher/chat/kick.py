"""Kick live chat over the Pusher WebSocket (unofficial, UNVERIFIED against the live service).

The Pusher app key and the channel API are not documented by Kick; both can change. ``websockets`` is imported
lazily. The channel lookup may be behind Cloudflare: pass ``headers_provider`` (e.g. returning cookies from a
patchright session) to ``resolve_chatroom_id``.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.request
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from clipwatcher import config
from clipwatcher.chat.twitch_irc import backoff_delay
from clipwatcher.models import ChatMessage

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
