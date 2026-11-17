"""Append-only JSONL chat log for live streams: one line per message, flushed immediately."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path

from clipwatcher import config
from clipwatcher.models import ChatMessage


def default_root() -> Path:
    return config.ROOT / "data" / "chat"


class LiveLog:
    """data/chat/<login>/<stream_id>.jsonl (same line format as chat.convert.write_jsonl)."""

    def __init__(self, login: str, stream_id: str, root: Path | None = None):
        self.path = Path(root or default_root()) / login.lower() / f"{stream_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, msg: ChatMessage) -> None:
        self._fh.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> LiveLog:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


async def logged(source: AsyncIterable[ChatMessage], log: LiveLog) -> AsyncIterator[ChatMessage]:
    """Tee: write every message to ``log`` before passing it on."""
    async for msg in source:
        log.write(msg)
        yield msg
