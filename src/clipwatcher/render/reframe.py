"""Vertical reframe for full-camera and IRL clips (owner, 18 Nov).

Clip channels (speeduniverse, deagzzzshorts) fill the whole 9:16 screen with the person, the camera
panning to follow them. A 16:9 IRL shot boxed in the middle of a blurred background looks like a repost.
So when the clip has no webcam overlay and a real face is on screen most of the time, the renderer crops
a full-height 9:16 window that follows the main face:

- one frame every SAMPLE_S, the largest face (closest to the previous one when several are on screen);
- gaps held, a median filter against detector jitter, a dead zone so the camera does not drift;
- the path becomes a piecewise-linear ffmpeg expression in t for crop x.

Gameplay with no camera stays in the boxed frame: faces are rare there, so coverage stays under MIN_COVER.
"""

from __future__ import annotations

import logging
import statistics
import subprocess
import tempfile
from pathlib import Path

from clipwatcher import calibrate, config
from clipwatcher.render import seek

log = logging.getLogger(__name__)

SAMPLE_S = 0.5
MIN_COVER = 0.4  # share of samples with a face needed to reframe
MIN_FACE_H = 0.07  # faces smaller than this share of the frame height are crowd, not the subject
DEAD_ZONE = 0.06  # the window moves only when the face drifts this far (share of frame width)
MAX_SPEED = 0.35  # pan speed limit, frame widths per second: a whip pan in half a second looks broken
MAX_POINTS = 24


def enabled() -> bool:
    return bool(config.global_config().get("render", {}).get("reframe", True))


def _frames(media: Path, start: float, dur: float, out: Path) -> list[tuple[float, Path]]:
    """Frames every SAMPLE_S over [start, start+dur] in one pass (D9: coarse seek, trim in the graph)."""
    inp, trim = seek.one_stream(media, start)
    vf = f"trim=start={trim:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS,fps={1 / SAMPLE_S:g},scale=-2:360"
    subprocess.run(
        [config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", *inp, "-vf", vf, "-an",
         str(out / "f_%04d.png")],
        capture_output=True, timeout=180,
    )  # fmt: skip
    return [(i * SAMPLE_S, p) for i, p in enumerate(sorted(out.glob("f_*.png")))]


def track(media: Path, start: float, dur: float, detector=None) -> list[tuple[float, float | None]]:
    """[(t, face centre x as a share of the frame width or None)] every SAMPLE_S, t relative to start."""
    detector = detector or calibrate.make_detector()
    if detector is None:
        return []
    from PIL import Image

    out: list[tuple[float, float | None]] = []
    prev: float | None = None
    with tempfile.TemporaryDirectory(prefix="cw_reframe_") as td:
        for t, p in _frames(Path(media), start, dur, Path(td)):
            with Image.open(p) as im:
                fw, fh = im.size
            faces = [b for b in detector(p) if b[3] >= MIN_FACE_H * fh]
            if not faces:
                out.append((t, None))
                continue
            big = max(f[3] for f in faces)
            cands = [f for f in faces if f[3] >= 0.6 * big]  # the subject, not the passer-by
            pick = (
                min(cands, key=lambda f: abs((f[0] + f[2] / 2) / fw - prev))
                if prev is not None
                else max(cands, key=lambda f: f[3])
            )
            prev = (pick[0] + pick[2] / 2) / fw
            out.append((t, round(prev, 4)))
    return out


def coverage(samples: list[tuple[float, float | None]]) -> float:
    return sum(1 for _, x in samples if x is not None) / len(samples) if samples else 0.0


def smooth(samples: list[tuple[float, float | None]]) -> list[tuple[float, float]]:
    """Gaps held (first face backfilled), 5-sample median, dead zone; centre x shares of the frame width."""
    xs = [x for _, x in samples]
    first = next((x for x in xs if x is not None), 0.5)
    filled, last = [], first
    for x in xs:
        last = x if x is not None else last
        filled.append(last)
    med = [statistics.median(filled[max(0, i - 2) : i + 3]) for i in range(len(filled))]
    out, cur = [], med[0] if med else 0.5
    for (t, _), x in zip(samples, med, strict=True):
        if abs(x - cur) > DEAD_ZONE:
            target = x - DEAD_ZONE if x > cur else x + DEAD_ZONE
            step = MAX_SPEED * SAMPLE_S
            cur = min(max(target, cur - step), cur + step)
        out.append((t, round(cur, 4)))
    return out


def keyframes(path: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Fewer points for the ffmpeg expression: keep the ends and every turn of direction or hold."""
    if len(path) <= 2:
        return path
    keep = [path[0]]
    for a, b, c in zip(path, path[1:], path[2:], strict=False):
        d1, d2 = b[1] - a[1], c[1] - b[1]
        if (d1 > 0) != (d2 > 0) or (d1 == 0) != (d2 == 0):
            keep.append(b)
    keep.append(path[-1])
    while len(keep) > MAX_POINTS:  # drop the smallest moves
        i = min(range(1, len(keep) - 1), key=lambda k: abs(keep[k][1] - keep[k - 1][1]))
        keep.pop(i)
    return keep


def x_expr(points: list[tuple[float, float]], src_w: int, crop_w: int) -> str:
    """ffmpeg crop x(t): the window centred on the path, clamped inside the frame, linear between points."""

    def px(c: float) -> float:
        return round(min(max(c * src_w - crop_w / 2, 0), src_w - crop_w), 1)

    if not points:
        return str(px(0.5))
    expr = str(px(points[-1][1]))
    for (t0, c0), (t1, c1) in reversed(list(zip(points, points[1:], strict=False))):
        a, b = px(c0), px(c1)
        seg = f"{a}+({b - a:.1f})*(t-{t0:.2f})/{max(t1 - t0, 0.01):.2f}" if a != b else f"{a}"
        expr = f"if(lt(t,{t1:.2f}),{seg},{expr})"
    return f"if(lt(t,{points[0][0]:.2f}),{px(points[0][1])},{expr})"
