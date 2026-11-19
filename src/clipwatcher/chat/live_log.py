"""Append-only JSONL chat log for live streams: one line per message, flushed immediately.

One file per broadcast (``<stream id>.jsonl``). Unlike the video ring buffer nothing here is ever pruned:
the whole chat of a stream stays on disk, is read back after a restart and is what the spike baseline is
built from (``clipwatcher.session``).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from pathlib import Path

from clipwatcher import config
from clipwatcher.models import ChatMessage


def default_root() -> Path:
    return config.ROOT / "work" / "chat"


class LiveLog:
    """<root>/<login>/<stream_id>.jsonl (same line format as chat.convert.write_jsonl)."""

    def __init__(self, login: str, stream_id: str, root: Path | None = None):
        self.path = Path(root or default_root()) / login.lower() / f"{stream_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, msg: ChatMessage) -> None:
        self._fh.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
        self._fh.flush()

    def read(self) -> Iterator[ChatMessage]:
        """Everything saved so far, in file order. A line torn by a crash mid-write is skipped."""
        self._fh.flush()
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield ChatMessage(**json.loads(line))
                except (ValueError, TypeError):
                    continue

    def rewrite(self, messages: Iterable[ChatMessage]) -> None:
        """Replace the file with ``messages`` (history merged with a backfill), then keep appending."""
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m.to_dict(), ensure_ascii=False) + "\n")
        self._fh.close()
        os.replace(tmp, self.path)
        self._fh = self.path.open("a", encoding="utf-8")

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
