"""Chat conversion: TwitchDownloaderCLI chatdownload JSON <-> unified ChatMessage JSONL."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from clipwatcher import config
from clipwatcher.models import ChatMessage

_NOTICE_TYPES = {
    "sub",
    "resub",
    "subgift",
    "submysterygift",
    "giftpaidupgrade",
    "anongiftpaidupgrade",
    "raid",
    "unraid",
    "ritual",
    "bitsbadgetier",
}


def bot_names() -> set[str]:
    return {b.lower() for b in config.global_config().get("detector", {}).get("bots", [])}


def _notice_of(msg: dict) -> tuple[str | None, int | None]:
    params = msg.get("user_notice_params") or {}
    mid = params.get("msg-id") or msg.get("msg-id")
    viewers = params.get("msg-param-viewerCount")
    try:
        viewers = int(viewers) if viewers is not None else None
    except (TypeError, ValueError):
        viewers = None
    if mid and (mid in _NOTICE_TYPES or params):
        return str(mid), viewers
    return None, viewers


def tdl_to_messages(path: str | Path, bots: Iterable[str] | None = None) -> list[ChatMessage]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    botset = {b.lower() for b in bots} if bots is not None else bot_names()
    out: list[ChatMessage] = []
    for c in data.get("comments", []):
        msg = c.get("message") or {}
        commenter = c.get("commenter") or {}
        user = (commenter.get("name") or commenter.get("display_name") or "").lower()
        text = msg.get("body")
        if text is None:
            text = "".join(f.get("text", "") for f in msg.get("fragments", []) or [])
        ntype, viewers = _notice_of(msg)
        out.append(
            ChatMessage(
                ts_ms=int(round(float(c.get("content_offset_seconds", 0)) * 1000)),
                user=user,
                text=text,
                is_bot=user in botset,
                is_notice=ntype is not None,
                notice_type=ntype,
                viewers=viewers,
            )
        )
    out.sort(key=lambda m: m.ts_ms)
    return out


def write_jsonl(messages: Iterable[ChatMessage], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(asdict(m), ensure_ascii=False) + "\n")
    return p


def read_jsonl(path: str | Path) -> list[ChatMessage]:
    """Chat log -> messages. A line torn by a crash mid-write (live logs are appended while the stream runs)
    is skipped instead of failing the whole file."""
    out: list[ChatMessage] = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(ChatMessage(**json.loads(line)))
            except (ValueError, TypeError):
                continue
    return out
