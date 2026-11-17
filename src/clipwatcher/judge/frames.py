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
    paths: list[Path] = []
    for i in range(n):
        seek, t = _seek_for(start_s + dur_s * (i + 0.5) / n)
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


def _seek_for(abs_s: float) -> tuple[float, float]:
    """D9 per frame: coarse input seek to 6 s before this frame, exact trim in the graph.

    Seeking once to the window start made every frame decode the whole window up to its own time
    (8 frames of a 45 s 1080p60 clip = ~200 s of decoding); now each frame decodes at most ~6 s.
    """
    seek = max(0.0, abs_s - SEEK_PAD_S)
    return seek, abs_s - seek


def frame_times(dur_s: float, n: int = 8, peak_rel: float | None = None, n_peak: int = 2) -> list[float]:
    """Evenly spaced times plus extra frames just after the chat peak (the reaction), sorted, deduped."""
    n_even = max(1, n - (n_peak if peak_rel is not None else 0))
    times = [dur_s * (i + 0.5) / n_even for i in range(n_even)]
    if peak_rel is not None:
        times += [peak_rel + 0.5 + 1.5 * i for i in range(n_peak)]
    out: list[float] = []
    for t in sorted(min(max(t, 0.0), max(dur_s - 0.05, 0.0)) for t in times):
        if not out or t - out[-1] >= 0.4:
            out.append(round(t, 1))
    return out


def keyframes_lowres(
    media: str | Path,
    start_s: float,
    dur_s: float,
    *,
    n: int = 8,
    peak_rel: float | None = None,
    size: tuple[int, int] = (480, 270),
    quality: int = 65,
    out_dir: Path | None = None,
) -> list[tuple[float, Path]]:
    """[(t, jpeg)] at `size` (letterboxed), JPEG quality 65, no EXIF. t is relative to start_s.

    480x270 costs ~173 vision tokens per frame (w*h/750) instead of ~546 at 853x480 and ~1,229 at 720p,
    and webcam faces and on-screen action stay readable. Coarse seek + trim in the graph (D9).
    """
    from PIL import Image

    out_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="cw_lowres_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = size
    res: list[tuple[float, Path]] = []
    for i, t in enumerate(frame_times(dur_s, n, peak_rel)):
        seek, trim = _seek_for(start_s + t)
        png = out_dir / f"f{i:02d}.png"
        vf = (
            f"trim=start={trim:.3f},setpts=PTS-STARTPTS,"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=area,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
        )
        cmd = [
            config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{seek:.3f}", "-i", str(media), "-vf", vf, "-frames:v", "1", str(png),
        ]  # fmt: skip
        subprocess.run(cmd, check=True, capture_output=True)
        if not png.exists():
            continue
        jpg = out_dir / f"f{i:02d}.jpg"
        with Image.open(png) as im:
            im.convert("RGB").save(
                jpg, "JPEG", quality=quality, optimize=True
            )  # no exif passed -> none written
        png.unlink(missing_ok=True)
        res.append((t, jpg))
    return res


def vision_tokens(width: int, height: int) -> int:
    """Approximate image input tokens: width*height/750 (documented estimate)."""
    return round(width * height / 750)
