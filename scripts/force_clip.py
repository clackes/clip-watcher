#!/usr/bin/env python3
"""Push one forced candidate through the live pipeline using a running ring buffer (end-to-end check).

.venv/bin/python scripts/force_clip.py work/ring/<login>/<stream_id> <login> [--secs-ago 40] [--dur 30]
"""

import argparse
import asyncio
import time
from pathlib import Path

from clipwatcher.fastlane import FastLane
from clipwatcher.ingest.ringbuffer import RingBuffer
from clipwatcher.models import Candidate
from clipwatcher.queue.db import ClipDB
from clipwatcher.queue.staging import staging_root


async def _empty():
    if False:
        yield


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ring")
    ap.add_argument("login")
    ap.add_argument("--secs-ago", type=float, default=45)
    ap.add_argument("--dur", type=float, default=30)
    a = ap.parse_args()
    ring = RingBuffer(Path(a.ring))
    end = min(time.time(), ring.covered_until_ms() / 1000) - 6
    start = max(end - a.dur, ring.segments()[0]["start_ms"] / 1000 + 1)
    cand = Candidate(a.login, start + 20, start, end, chat_score=9.9, final_score=9.9)
    root = staging_root()
    root.mkdir(parents=True, exist_ok=True)
    lane = FastLane(a.login, _empty(), None, ring, db=ClipDB(root / "clips.db"), staging_root=root)
    t0 = time.time()
    out = asyncio.run(lane.process(cand))
    print(out.status, out.path, out.timings, f"wall={time.time() - t0:.1f}s", out.error or "")


if __name__ == "__main__":
    main()
