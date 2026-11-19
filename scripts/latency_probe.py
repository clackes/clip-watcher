"""Measure chat latency against the recorded video on a live stream that shows chat on screen.

Cuts the last --seconds of the ring buffer, writes one frame per second named by its wall-clock time
(our segment clock, epoch ms), and the IRC messages of that span with their tmi-sent-ts. A message
that shows up in the on-screen chat at frame time F with IRC time T gives our capture latency F - T
(the overlay draws it at the streamer's PC within ~1 s of T). The gap between an on-screen action
and the on-screen chat reacting to it is the viewers' latency + reaction time.

    python scripts/latency_probe.py xqc --seconds 60 --out work/probe
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from clipwatcher import config
from clipwatcher.ingest.ringbuffer import RingBuffer
from clipwatcher.live import data_root


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("streamer")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--ago", type=float, default=300, help="end this many s before the newest segment")
    ap.add_argument("--out", type=Path, default=Path("work/probe"))
    a = ap.parse_args()
    login = a.streamer.lower()
    runs = sorted((data_root() / "ring" / login).glob("*/index.jsonl"), key=lambda p: p.stat().st_mtime)
    ring = RingBuffer(runs[-1].parent)
    segs = ring.segments()
    end_ms = segs[-1]["end_ms"] - int(a.ago * 1000)  # the chat log is flushed in blocks
    start_ms = end_ms - int(a.seconds * 1000)
    out = a.out / login
    out.mkdir(parents=True, exist_ok=True)
    src, local = ring.cut(start_ms, end_ms, out / "src.ts")
    base_ms = start_ms - int(local * 1000)  # wall clock of the first cut frame
    subprocess.run(
        [config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vf", "fps=1,scale=1280:-2", "-q:v", "4", str(out / "f%04d.jpg")],
        check=True,
    )  # fmt: skip
    frames = sorted(out.glob("f*.jpg"))
    for i, f in enumerate(frames):  # fps=1 picks the frame at t = i + 0.5 s
        f.rename(out / f"{base_ms + i * 1000 + 500}.jpg")
    logs = sorted((data_root() / "chat" / login).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    lines = []
    for ln in logs[-1].read_text(encoding="utf-8").splitlines():
        m = json.loads(ln)
        if base_ms - 120_000 <= m["ts_ms"] <= end_ms + 5_000:
            lines.append(f"{m['ts_ms']} {m['user']}: {m['text']}")
    (out / "chat.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"{login}: {len(frames)} frames from {base_ms} to {end_ms}, {len(lines)} chat lines -> {out}")


if __name__ == "__main__":
    main()
