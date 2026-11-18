"""Still stretches at the edges of a clip: a capture card's "NO SIGNAL" card, a black or frozen screen.

Owner, 18 Nov: a jasontheween clip opened on 2 s of Elgato "NO SIGNAL" ("not funny"). The whole frame does
not change there, which never happens while a streamer is on cam or a game runs, so ffmpeg freezedetect over
the clip finds it and the trim skips it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..config import ffmpeg_bin

EDGE_S = 0.3  # a freeze starting this close to the start (ending this close to the end) is at the edge
_START = re.compile(r"freeze_start: ([\d.]+)")
_END = re.compile(r"freeze_end: ([\d.]+)")


def freezes(stderr: str) -> list[tuple[float, float | None]]:
    """(start, end) of every freeze in ffmpeg freezedetect output; end None = frozen until the end."""
    starts = [float(x) for x in _START.findall(stderr)]
    ends = [float(x) for x in _END.findall(stderr)]
    return [(s, ends[i] if i < len(ends) else None) for i, s in enumerate(starts)]


def edges(spans: list[tuple[float, float | None]], dur: float) -> tuple[float, float]:
    """Seconds to drop at the head and the tail of a dur-long clip."""
    head = tail = 0.0
    for s, e in spans:
        if s <= EDGE_S:
            head = max(head, e if e is not None else dur)
        if e is None or e >= dur - EDGE_S:
            tail = max(tail, dur - s)
    return head, tail


def still_edges(media: Path, start_s: float, dur_s: float, min_s: float = 0.8) -> tuple[float, float]:
    """(head, tail) seconds of a frozen picture at the clip's edges; (0, 0) when none or on any error.
    D9: coarse input seek, exact trim in the graph."""
    coarse = max(0.0, start_s - 6)
    vf = (
        f"trim=start={start_s - coarse:.3f}:duration={dur_s:.3f},setpts=PTS-STARTPTS,"
        f"scale=320:-2,freezedetect=n=0.003:d={min_s}"
    )
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-ss", f"{coarse:.3f}", "-i", str(media), "-an",
           "-vf", vf, "-f", "null", "-"]  # fmt: skip
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return 0.0, 0.0
    if r.returncode != 0:
        return 0.0, 0.0
    return edges(freezes(r.stderr), dur_s)
