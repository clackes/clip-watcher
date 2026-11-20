"""What a run is doing, for whoever is watching it (the companion app, ``python -m clipwatcher.ui``).

The lanes call ``emit``. With CLIPWATCHER_EVENTS unset nothing happens, so a bot started from a terminal or a
service behaves exactly as before. With it set to a file, every event is one JSON object on its own line,
appended to that file: {"t": epoch seconds, "kind": ..., ...}.

Kinds the lanes send: recording, baseline, watching, moment, step (state start | done | failed), judged,
staged, rejected, failed, chat (VOD lane: messages read), candidates.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def path() -> Path | None:
    p = os.environ.get("CLIPWATCHER_EVENTS")
    return Path(p) if p else None


def emit(kind: str, **data: Any) -> None:
    p = path()
    if p is None:
        return
    row = {"t": round(time.time(), 3), "kind": kind, **data}
    try:
        with _lock, p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # a full disk or a removed folder must never stop a clip


@contextmanager
def step(name: str, **data: Any) -> Iterator[None]:
    """A step of a lane: start, then done with its seconds, or failed with the error."""
    emit("step", step=name, state="start", **data)
    t0 = time.perf_counter()
    try:
        yield
    except BaseException as e:
        took = round(time.perf_counter() - t0, 2)
        emit("step", step=name, state="failed", seconds=took, error=f"{type(e).__name__}: {e}"[:300], **data)
        raise
    emit("step", step=name, state="done", seconds=round(time.perf_counter() - t0, 2), **data)
