"""Scheduled auto-publishing (owner, 19 Nov): the channel posts by itself.

At most `publish.schedule.max_per_day` Shorts in any 24 h, at least `min_gap_min` apart, always the best
judged clip waiting in the queue (READY, scored by the judge, fresh). Operators can still Publish or Discard
any card in Telegram; their uploads count toward the same daily cap.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .. import config
from .db import ClipDB

log = logging.getLogger(__name__)
DAY_S = 24 * 3600


def settings() -> dict[str, Any]:
    return dict(config.global_config().get("publish", {}).get("schedule") or {})


def pick(db: ClipDB, now: float, cfg: dict[str, Any]) -> str | None:
    """The clip to publish now, or None (off, cap reached, too soon, nothing good enough waiting)."""
    if not cfg.get("enabled"):
        return None
    cap = int(cfg.get("max_per_day", 6))
    done = db.published_since(now - DAY_S)
    if len(done) >= cap:
        return None
    if done and now - done[-1] < float(cfg.get("min_gap_min", 180)) * 60:
        return None
    min_score = float(cfg.get("min_score", 6))
    max_age = float(cfg.get("max_age_h", 24)) * 3600
    best: dict[str, Any] | None = None
    for c in db.list_clips("READY"):
        meta = db.meta(c)
        if meta.get("judge_source") in ("heuristic", "none", None):
            continue  # only clips the judge actually watched go out unattended
        if (c["score"] or 0) < min_score or now - (c["created_at"] or 0) > max_age:
            continue
        if best is None or (c["score"] or 0) > (best["score"] or 0):
            best = c
    return best["id"] if best else None


async def run(
    db: ClipDB,
    upload: Callable[[str, int, int], Awaitable[Any]],
    *,
    every_s: float = 300,
    retry_s: float = 3600,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    once: bool = False,
) -> None:
    """Every every_s: approve the picked clip and hand it to upload(clip_id, n_today, cap).

    A failed upload (YouTube signed out, network) puts the clip back in the queue and pauses for retry_s, so
    one outage does not burn through every waiting clip or flood Telegram with errors.
    """
    from . import staging

    while True:
        wait = every_s
        try:
            cfg = settings()
            cid = pick(db, clock(), cfg)
            if cid and db.approve_scheduled(cid):
                staging.mark_approved(cid, "auto")
                n = len(db.published_since(clock() - DAY_S)) + 1
                log.info("schedule: publishing %s (%d/%s today)", cid, n, cfg.get("max_per_day", 6))
                if not await upload(cid, n, int(cfg.get("max_per_day", 6))):
                    if db.state(cid) == "APPROVED":
                        db.set_state(cid, "READY", actor="schedule")
                    log.warning("schedule: upload of %s failed; back in the queue, retry in %.0f min",
                                cid, retry_s / 60)  # fmt: skip
                    wait = retry_s
        except Exception:  # noqa: BLE001 - the scheduler must outlive any one bad clip
            log.exception("schedule: publish round failed")
            wait = retry_s
        if once:
            return
        await sleep(wait)
