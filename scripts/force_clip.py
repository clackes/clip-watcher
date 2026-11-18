#!/usr/bin/env python3
"""Push one forced candidate through the live pipeline using a running ring buffer (end-to-end check).

.venv/bin/python scripts/force_clip.py work/ring/<login>/<stream_id> <login> [--secs-ago 6] [--dur 30]
    [--telegram]

--secs-ago: the forced moment ends this many seconds before the newest recorded video.
--telegram: also post the approval card (needs TELEGRAM_BOT_TOKEN), like a real live candidate.
"""

import argparse
import asyncio
import time
from pathlib import Path

from clipwatcher.fastlane import FastLane
from clipwatcher.ingest.ringbuffer import RingBuffer
from clipwatcher.models import Candidate
from clipwatcher.queue.db import ClipDB


async def _empty():
    if False:
        yield


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ring")
    ap.add_argument("login")
    ap.add_argument("--secs-ago", type=float, default=6)
    ap.add_argument("--dur", type=float, default=30)
    ap.add_argument("--telegram", action="store_true")
    a = ap.parse_args()
    ring = RingBuffer(Path(a.ring))
    end = min(time.time(), ring.covered_until_ms() / 1000) - a.secs_ago
    start = max(end - a.dur, ring.segments()[0]["start_ms"] / 1000 + 1)
    cand = Candidate(a.login, start + 20, start, end, chat_score=9.9, final_score=9.9)
    root = Path("staging")
    root.mkdir(exist_ok=True)
    db = ClipDB(root / "clips.db")

    async def run():
        bot = None
        if a.telegram:
            from clipwatcher.queue.telegram_bot import build_application

            bot = build_application(db)
            await bot.initialize()
        try:
            lane = FastLane(a.login, _empty(), None, ring, db=db, bot=bot, staging_root=root)
            return await lane.process(cand)
        finally:
            if bot is not None:
                await bot.shutdown()

    t0 = time.time()
    out = asyncio.run(run())
    print(out.status, out.path, out.timings, f"wall={time.time() - t0:.1f}s", out.error or "")


if __name__ == "__main__":
    main()
