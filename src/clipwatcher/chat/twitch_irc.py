"""Anonymous Twitch IRC reader (TLS, IRCv3 tags) yielding unified ChatMessage objects.

Pure stdlib (asyncio + ssl). Heartbeat: own PING every ``ping_interval`` s, reconnect when no PONG arrives
within ``pong_timeout`` s. The server RECONNECT command and dropped sockets reconnect with jittered
exponential backoff.
"""

from __future__ import annotations

import asyncio
import random
import ssl
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from clipwatcher import config
from clipwatcher.models import ChatMessage

HOST = "irc.chat.twitch.tv"
PORT = 6697
_UNESC = {"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}


@dataclass
class IrcLine:
    tags: dict[str, str] = field(default_factory=dict)
    prefix: str = ""
    command: str = ""
    params: list[str] = field(default_factory=list)  # last param is the trailing text when present

    @property
    def nick(self) -> str:
        return self.prefix.split("!", 1)[0]

    @property
    def text(self) -> str:
        return self.params[-1] if len(self.params) > 1 else ""


def unescape_tag(v: str) -> str:
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            out.append(_UNESC.get(v[i + 1], v[i + 1]))
            i += 2
            continue
        if c != "\\":
            out.append(c)
        i += 1
    return "".join(out)


def parse_line(raw: str) -> IrcLine:
    s = raw.rstrip("\r\n")
    line = IrcLine()
    if s.startswith("@"):
        tagstr, _, s = s.partition(" ")
        for kv in tagstr[1:].split(";"):
            k, _, v = kv.partition("=")
            line.tags[k] = unescape_tag(v)
    if s.startswith(":"):
        line.prefix, _, s = s[1:].partition(" ")
    head, sep, trailing = s.partition(" :")
    parts = head.split()
    if parts:
        line.command, line.params = parts[0].upper(), parts[1:]
    if sep:
        line.params.append(trailing)
    return line


def line_to_message(line: IrcLine, bots: set[str] | frozenset[str] = frozenset()) -> ChatMessage | None:
    """PRIVMSG -> chat line, USERNOTICE -> notice (raid / subgift / submysterygift / sub ...)."""
    if line.command not in ("PRIVMSG", "USERNOTICE"):
        return None
    tags = line.tags
    try:
        ts = int(tags["tmi-sent-ts"])
    except (KeyError, ValueError):
        ts = int(time.time() * 1000)
    text = line.text
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[8:-1]
    if line.command == "PRIVMSG":
        user = (tags.get("display-name") or line.nick).lower()
        return ChatMessage(ts_ms=ts, user=user, text=text, platform="twitch", is_bot=user in bots)
    user = (tags.get("login") or tags.get("display-name") or line.nick).lower()
    viewers = None
    for key in ("msg-param-viewerCount", "msg-param-mass-gift-count"):
        if tags.get(key, "").isdigit():
            viewers = int(tags[key])
            break
    return ChatMessage(
        ts_ms=ts,
        user=user,
        text=text,
        platform="twitch",
        is_bot=user in bots,
        is_notice=True,
        notice_type=tags.get("msg-id") or None,
        viewers=viewers,
    )


def backoff_delay(
    attempt: int, base: float = 1.0, cap: float = 60.0, rng: Callable[[], float] = random.random
):
    """Jittered exponential backoff (50-100% of the exponential value)."""
    return min(cap, base * (2 ** max(0, attempt))) * (0.5 + 0.5 * rng())


class _Reconnect(Exception):
    pass


class TwitchChat:
    """``async for msg in TwitchChat("channel"): ...`` Runs until ``close()`` or task cancellation."""

    def __init__(
        self,
        channel: str,
        *,
        host: str = HOST,
        port: int = PORT,
        use_tls: bool = True,
        ssl_context: ssl.SSLContext | None = None,
        bots: set[str] | None = None,
        ping_interval: float = 60.0,
        pong_timeout: float = 10.0,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
    ):
        self.channel = channel.lower().lstrip("#")
        self.host, self.port, self.use_tls, self._ssl = host, port, use_tls, ssl_context
        if bots is None:
            bots = {b.lower() for b in config.global_config().get("detector", {}).get("bots", [])}
        self.bots = bots
        self.ping_interval, self.pong_timeout = ping_interval, pong_timeout
        self.backoff_base, self.backoff_cap = backoff_base, backoff_cap
        self.nick = f"justinfan{random.randint(10000, 99999999)}"
        self.connects = 0
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def __aiter__(self) -> AsyncIterator[ChatMessage]:
        return self.messages()

    async def _open(self):
        ctx = (self._ssl or ssl.create_default_context()) if self.use_tls else None
        r, w = await asyncio.open_connection(self.host, self.port, ssl=ctx)
        self.connects += 1
        for cmd in (
            "CAP REQ :twitch.tv/tags twitch.tv/commands",
            "PASS SCHMOOPIIE",
            f"NICK {self.nick}",
            f"JOIN #{self.channel}",
        ):
            w.write(f"{cmd}\r\n".encode())
        await w.drain()
        return r, w

    async def _session(self) -> AsyncIterator[ChatMessage]:
        reader, writer = await self._open()
        loop = asyncio.get_running_loop()
        next_ping = loop.time() + self.ping_interval
        pong_deadline: float | None = None
        try:
            while not self._closed:
                now = loop.time()
                due = pong_deadline if pong_deadline is not None else next_ping
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=max(0.0, due - now))
                except TimeoutError:
                    if pong_deadline is not None:
                        raise _Reconnect("pong timeout") from None
                    writer.write(b"PING :tmi.twitch.tv\r\n")
                    await writer.drain()
                    pong_deadline = loop.time() + self.pong_timeout
                    next_ping = loop.time() + self.ping_interval
                    continue
                if not raw:
                    raise _Reconnect("eof")
                line = parse_line(raw.decode("utf-8", "replace"))
                if line.command == "PING":
                    writer.write(f"PONG :{line.text or 'tmi.twitch.tv'}\r\n".encode())
                    await writer.drain()
                elif line.command == "PONG":
                    pong_deadline = None
                elif line.command == "RECONNECT":
                    raise _Reconnect("server requested reconnect")
                else:
                    msg = line_to_message(line, self.bots)
                    if msg:
                        yield msg
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - socket may already be dead
                pass

    async def messages(self) -> AsyncIterator[ChatMessage]:
        attempt = 0
        while not self._closed:
            try:
                async for msg in self._session():
                    attempt = 0
                    yield msg
                    if self._closed:
                        return
            except (_Reconnect, OSError, ssl.SSLError, asyncio.IncompleteReadError):
                pass
            if self._closed:
                return
            await asyncio.sleep(backoff_delay(attempt, self.backoff_base, self.backoff_cap))
            attempt += 1
