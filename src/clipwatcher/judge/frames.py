"""Evenly spaced JPEG keyframes of a candidate window."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .. import config

SEEK_PAD_S = 6.0


def keyframes(
    media: str | Path,
    start_s: float,
    dur_s: float,
    n: int = 6,
    height: int = 480,
    out_dir: Path | None = None,
) -> list[Path]:
    """n JPEGs at `height`. Coarse seek, then trim in the graph (D9)."""
    out_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="cw_frames_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    seek = max(0.0, start_s - SEEK_PAD_S)
    trim = start_s - seek
    paths: list[Path] = []
    for i in range(n):
        t = trim + dur_s * (i + 0.5) / n
        out = out_dir / f"frame_{i:02d}.jpg"
        vf = f"trim=start={t:.3f},setpts=PTS-STARTPTS,scale=-2:{height}"
        cmd = [
            config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{seek:.3f}", "-i", str(media), "-vf", vf,
            "-frames:v", "1", "-q:v", "3", str(out),
        ]  # fmt: skip
        subprocess.run(cmd, check=True, capture_output=True)
        if out.exists():
            paths.append(out)
    return paths
