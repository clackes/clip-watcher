"""Every judge call, one JSON line each: which task went to which model, the tokens it used, what it cost at
the prices of that day (judge.prices in config/global.yaml) and what the judge answered. The companion app
reads it for cost tracking and for the judge's history; nothing else depends on it.

The file is work/usage/judge.jsonl, or CLIPWATCHER_LEDGER. A call that failed is written too, with its
error and no tokens, so the history shows when the fallback took over and why.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from clipwatcher import config

_lock = threading.Lock()
TOKENS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_PRICE_OF = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_write",
}


def path() -> Path:
    env = os.environ.get("CLIPWATCHER_LEDGER")
    return Path(env).expanduser() if env else config.ROOT / "work" / "usage" / "judge.jsonl"


def cost_parts(model: str, usage: dict[str, int]) -> dict[str, float] | None:
    """USD by kind of token for one call, or None when the price list does not know the model."""
    prices = (config.global_config().get("judge", {}).get("prices") or {}).get(model)
    if not prices:
        return None
    return {
        _PRICE_OF[k]: round(int(usage.get(k, 0) or 0) * float(prices.get(_PRICE_OF[k], 0)) / 1e6, 8)
        for k in TOKENS
    }


def record(
    *,
    task: str,
    model: str,
    usage: dict[str, int] | None = None,
    streamer: str = "",
    start_s: float | None = None,
    error: str = "",
    **answer: Any,
) -> None:
    """Append one call. ``answer``: what the judge said (ok, score, flags, why, title), kept as given."""
    usage = {k: int((usage or {}).get(k, 0) or 0) for k in TOKENS}
    parts = cost_parts(model, usage)
    row = {
        "t": round(time.time(), 3), "task": task, "model": model, "streamer": streamer,
        "start_ms": None if start_s is None else int(start_s * 1000), **usage,
        "usd": None if parts is None else round(sum(parts.values()), 8), "usd_parts": parts,
        "error": error[:300], **answer,
    }  # fmt: skip
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock, p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # the ledger is bookkeeping: it must never cost a clip


def read() -> list[dict[str, Any]]:
    p = path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
