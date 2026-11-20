"""Facecam / gameplay rect calibration.

Samples frames from a recording, finds the streamer's face, derives the webcam overlay crop (2.25:1) and the
largest 2:1 gameplay region that avoids it, writes preview stills and optionally updates streamers.yaml.

CLI: python -m clipwatcher.calibrate --input media --streamer NAME [--write]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import statistics
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config
from .render import seek

log = logging.getLogger(__name__)

Box = tuple[float, float, float, float]  # x, y, w, h in source pixels
Rect = tuple[int, int, int, int]  # x, y, w, h
FaceDetector = Callable[[Path], list[Box]]

CAM_RATIO = 2.25
GAME_RATIO = 2.0
DEFAULT_FACE_MODEL = config.ROOT / "models" / "blaze_face_short_range.tflite"


# --------------------------------------------------------------------------- geometry (pure)
FULL_CAM_FACE_H = 0.18  # face taller than this share of the frame = full-screen camera
SAME_CAM_FACES = 1.0  # two face clusters closer than this many face widths are one face


@dataclass
class Rects:
    face: Rect | None
    facecam: Rect | None
    gameplay: Rect


def rect_str(r: Rect) -> str:
    """ffmpeg crop style "w:h:x:y" as stored in streamers.yaml."""
    x, y, w, h = r
    return f"{w}:{h}:{x}:{y}"


def _even(v: float) -> int:
    return int(v) // 2 * 2


def cluster_boxes(boxes: list[Box]) -> list[list[Box]]:
    """Greedy grouping by centre distance. Largest cluster first."""
    clusters: list[list[Box]] = []
    for b in boxes:
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        for c in clusters:
            mx = statistics.fmean(q[0] + q[2] / 2 for q in c)
            my = statistics.fmean(q[1] + q[3] / 2 for q in c)
            mw = statistics.fmean(q[2] for q in c)
            if math.hypot(cx - mx, cy - my) <= max(b[2], mw):
                c.append(b)
                break
        else:
            clusters.append([b])
    return sorted(clusters, key=len, reverse=True)


def dominant_face(boxes: list[Box], n_frames: int) -> Box | None:
    """Median box of the biggest cluster, if enough frames agree (a static webcam face)."""
    clusters = cluster_boxes(boxes)
    if not clusters:
        return None
    best = clusters[0]
    if len(best) < max(1, math.ceil(0.3 * n_frames)):
        return None
    return tuple(statistics.median(b[i] for b in best) for i in range(4))  # type: ignore[return-value]


def facecam_from_face(face: Box, fw: int, fh: int, snap: float = 0.08) -> Rect:
    """Widen a face box to the overlay: a 2.25:1 crop centred on the face, clamped (and edge-snapped)."""
    x, y, w, h = face
    ch = max(h * 2.2, w * 3 / CAM_RATIO)
    cw = ch * CAM_RATIO
    if cw > fw:
        cw, ch = fw, fw / CAM_RATIO
    if ch > fh:
        ch, cw = fh, fh * CAM_RATIO
    cw, ch = _even(cw), _even(ch)
    cx, cy = x + w / 2, y + h / 2
    left, top = cx - cw / 2, cy - ch / 2
    left = min(max(0.0, left), fw - cw)
    top = min(max(0.0, top), fh - ch)
    if left < snap * fw:
        left = 0.0
    elif fw - (left + cw) < snap * fw:
        left = fw - cw
    if top < snap * fh:
        top = 0.0
    elif fh - (top + ch) < snap * fh:
        top = fh - ch
    return _even(left), _even(top), cw, ch


def gameplay_avoiding(cam: Rect | None, fw: int, fh: int) -> Rect:
    """Largest 2:1 region not overlapping the facecam. Default (no cam): full width, centred."""
    full = (0, 0, fw, fh)
    regions = [full]
    if cam:
        cx, cy, cw, ch = cam
        regions = [
            (0, 0, cx, fh),  # left
            (cx + cw, 0, fw - cx - cw, fh),  # right
            (0, 0, fw, cy),  # above
            (0, cy + ch, fw, fh - cy - ch),  # below
        ]
    best: Rect | None = None
    best_score = 0.0
    for rx, ry, rw, rh in regions:
        if rw < 4 or rh < 2:
            continue
        w = _even(min(rw, rh * GAME_RATIO))
        h = _even(w / GAME_RATIO)
        if h < 2 or w < 4:
            continue
        r = (_even(rx + (rw - w) / 2), _even(ry + (rh - h) / 2), w, h)
        score = w * h * (1.3 if rh == fh else 1.0)  # prefer full-height side regions
        if best is None or score > best_score:
            best, best_score = r, score
    if best is None:  # cam covers everything: fall back to the full-frame 2:1
        w = _even(fw)
        h = _even(w / GAME_RATIO)
        return 0, _even((fh - h) / 2), w, h
    return best


def face_near(
    frames: list[Path], detector: FaceDetector, hint: tuple[float, float], fw: int, fh: int
) -> Box | None:
    """Face inside the webcam the judge pointed at, searched on a 2x zoom of that area (small, dark cams
    the full-frame pass misses). None when nothing is found there either."""
    from PIL import Image

    cw, ch = int(0.3 * fw), int(0.45 * fh)
    x0 = int(min(max(0, hint[0] * fw - cw / 2), fw - cw))
    y0 = int(min(max(0, hint[1] * fh - ch / 2), fh - ch))
    boxes: list[Box] = []
    for i, f in enumerate(frames):
        with Image.open(f) as im:
            k = im.width / fw
            crop = im.crop((int(x0 * k), int(y0 * k), int((x0 + cw) * k), int((y0 + ch) * k)))
            crop = crop.resize((crop.width * 2, crop.height * 2))
            q = f.with_name(f"near_{i:02d}.png")
            crop.save(q)
        boxes += [
            (x0 + bx / (2 * k), y0 + by / (2 * k), bw / (2 * k), bh / (2 * k))
            for bx, by, bw, bh in detector(q)
        ]
    return dominant_face(boxes, len(frames))


def gameplay_between(cams: list[Rect], fw: int, fh: int) -> Rect | None:
    """2:1 gameplay between the webcam columns on the left and right edges (multi-cam streams: the other
    people's cams stay out of the Short). None when that leaves less than 40% of the width."""
    left = max([x + w for x, _, w, _ in cams if x + w / 2 < fw / 2], default=0)
    right = min([x for x, _, w, _ in cams if x + w / 2 >= fw / 2], default=fw)
    w = _even(right - left)
    if w < 0.4 * fw:
        return None
    h = _even(min(w / GAME_RATIO, fh))
    w = _even(h * GAME_RATIO)
    return _even(left + (right - left - w) / 2), _even((fh - h) / 2), w, h


def compute_rects(boxes: list[Box], fw: int, fh: int, n_frames: int) -> Rects:
    face = dominant_face(boxes, n_frames)
    # A face taller than 18% of the frame is a full-screen camera (IRL / Just Chatting), not an overlay:
    # leave facecam unset so the template uses the no-webcam layout.
    full_cam = bool(face) and face[3] > FULL_CAM_FACE_H * fh
    cam = facecam_from_face(face, fw, fh) if face and not full_cam else None
    return Rects(
        face=tuple(int(v) for v in face) if face else None,  # type: ignore[arg-type]
        facecam=cam,
        gameplay=gameplay_avoiding(cam, fw, fh),
    )


def dedupe_boxes(boxes: list[Box], iou_min: float = 0.3) -> list[Box]:
    """Drop boxes that overlap an earlier, larger one (tiles see the same face twice)."""
    out: list[Box] = []
    for b in sorted(boxes, key=lambda b: -b[2] * b[3]):
        bx, by, bw, bh = b
        if all(
            max(0, min(bx + bw, ox + ow) - max(bx, ox)) * max(0, min(by + bh, oy + oh) - max(by, oy))
            < iou_min * min(bw * bh, ow * oh)
            for ox, oy, ow, oh in out
        ):
            out.append(b)
    return out


# --------------------------------------------------------------------------- detection backends
def make_detector(model_path: str | Path | None = None) -> FaceDetector | None:
    """MediaPipe Tasks FaceDetector, else OpenCV Haar, else None."""
    model = Path(
        model_path or config.global_config().get("calibrate", {}).get("face_model") or DEFAULT_FACE_MODEL
    )
    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        if model.exists():
            opts = vision.FaceDetectorOptions(
                base_options=mp_python.BaseOptions(model_asset_path=str(model)),
                min_detection_confidence=0.65,
            )
            det = vision.FaceDetector.create_from_options(opts)

            import numpy as np
            from PIL import Image

            def _run(arr) -> list[Box]:
                img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(arr))
                return [
                    (b.origin_x, b.origin_y, b.width, b.height)
                    for b in (d.bounding_box for d in det.detect(img).detections)
                ]

            def mediapipe_detect(p: Path) -> list[Box]:
                # The short-range model wants faces that fill much of the image, but webcam overlays are
                # small: also scan overlapping tiles (1/3 of the frame) and map boxes back.
                arr = np.asarray(Image.open(p).convert("RGB"))
                h, w = arr.shape[:2]
                boxes = list(_run(arr))
                tw, th = w // 3, h // 3
                for ty in range(0, h - th + 1, th // 2):
                    for tx in range(0, w - tw + 1, tw // 2):
                        for x, y, bw, bh in _run(arr[ty : ty + th, tx : tx + tw]):
                            boxes.append((x + tx, y + ty, bw, bh))
                return dedupe_boxes(boxes)

            return mediapipe_detect
        log.warning("face model %s missing; trying OpenCV", model)
    except (ImportError, OSError, RuntimeError) as e:  # e.g. libEGL missing in a slim container
        log.info("mediapipe unavailable (%s); trying OpenCV", e)
    try:
        import cv2

        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

        def haar_detect(p: Path) -> list[Box]:
            img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            found = cascade.detectMultiScale(img, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
            return [(float(x), float(y), float(w), float(h)) for x, y, w, h in found]

        return haar_detect
    except ImportError:
        return None


# --------------------------------------------------------------------------- frames + previews
def media_duration(media: Path) -> float:
    r = subprocess.run(
        [config.ffprobe_bin(), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(media)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return float(json.loads(r.stdout)["format"]["duration"])


def grab_frame(media: Path, t: float, out: Path, height: int | None = None) -> bool:
    """One PNG of the frame at t seconds. D9: a plain -ss on a .ts lands on a keyframe, often the one after
    t (six "peak" frames half a second apart came back as one picture), so the input seek is coarse and
    the frame is picked by trim in the graph (render/seek.py)."""
    inp, trim = seek.one_stream(media, max(0.0, t))
    vf = f"trim=start={trim:.3f},setpts=PTS-STARTPTS" + (f",scale=-2:{height}" if height else "")
    r = subprocess.run(
        [config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", *inp, "-vf", vf, "-frames:v", "1",
         str(out)],
        capture_output=True,
    )  # fmt: skip
    return r.returncode == 0 and out.exists()


def sample_frames(
    media: Path, out_dir: Path, n: int = 10, window: tuple[float, float] | None = None
) -> list[tuple[float, Path]]:
    """n PNG frames spread over the middle 90% of the media (single-frame grabs), or evenly over
    window = (start s, end s) when that lies inside the media: the stretch a clip is about, not the
    hours of VOD around it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = media_duration(media)
    lo, hi = 0.05 * dur, 0.95 * dur
    if window and min(dur, window[1]) - max(0.0, window[0]) >= 1.0:
        lo, hi = max(0.0, window[0]), min(dur, window[1])
    res = []
    for i in range(n):
        t = round(lo + (hi - lo) * (i + 0.5) / n, 3)
        p = out_dir / f"frame_{i:02d}.png"
        if grab_frame(media, t, p):
            res.append((t, p))
    return res


def frames_at(
    media: Path, out_dir: Path, times: list[float], height: int | None = 360
) -> list[tuple[float, Path]]:
    """Downscaled PNG frames at the given times (s), for motion checks around the peak."""
    out_dir.mkdir(parents=True, exist_ok=True)
    res = []
    for i, t in enumerate(times):
        p = out_dir / f"peak_{i:02d}.png"
        if grab_frame(media, t, p, height):
            res.append((t, p))
    return res


def stable_faces(
    boxes: list[Box], n_frames: int, fw: int, fh: int, min_share: float = 0.3, min_h: float = 0.04
) -> list[Box]:
    """One face per distinct webcam on screen, most-seen first.

    A face cluster counts when it is seen in >= min_share of the frames and is at least min_h of the frame
    tall (ID-card photos and HUD portraits are smaller). Clusters closer than SAME_CAM_FACES face widths are
    one face. Stacked cams sit close together, so objects detected as faces inside one cam are not dropped
    here: layout.reacting_face drops faces that are not detected steadily around the peak.
    """
    need = max(1, math.ceil(min_share * n_frames))
    out: list[Box] = []
    for c in cluster_boxes(boxes):
        if len(c) < need:
            continue
        f: Box = tuple(statistics.median(b[i] for b in c) for i in range(4))  # type: ignore[assignment]
        if f[3] < min_h * fh:
            continue
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
        if all(
            math.hypot(cx - o[0] - o[2] / 2, cy - o[1] - o[3] / 2) > SAME_CAM_FACES * max(f[2], o[2])
            for o in out
        ):
            out.append(f)
    return out


def face_motion(frames: list[Path], face: Box, fw: int, pad: float = 0.5) -> float:
    """Mean frame-to-frame pixel change around a face (source px box; frames may be downscaled).

    The streamer who reacts (laughs, shouts, jumps) moves; a co-streamer watching does not.
    """
    from PIL import Image, ImageChops, ImageStat

    diffs, prev = [], None
    for f in frames:
        with Image.open(f) as im:
            k = im.width / fw
            x, y, w, h = face
            pad_w, pad_h = w * pad, h * pad
            box = (
                int(max(0, (x - pad_w) * k)), int(max(0, (y - pad_h) * k)),
                int(min(im.width, (x + w + pad_w) * k)), int(min(im.height, (y + h + pad_h) * k)),
            )  # fmt: skip
            crop = im.convert("L").crop(box).resize((64, 64))
        if prev is not None:
            diffs.append(ImageStat.Stat(ImageChops.difference(crop, prev)).mean[0])
        prev = crop
    return statistics.fmean(diffs) if diffs else 0.0


# --------------------------------------------------------------------------- the webcam's own rectangle
# A face says where the streamer is, not where the webcam picture ends. A box guessed from the face size
# (facecam_from_face) cut wide cams down to the head, enlarging them 30 % more than needed, and reached
# past small cams into the game. The overlay itself is found from what makes it an overlay: its border
# stays on the same pixel rows and columns in every frame, while the picture on one or both sides changes.
EDGE_TAU = 8.0  # luma step that counts as an edge in one frame
SIDE_MIN = 0.45  # a side of the cam: an edge there in this share of (frames x its length)
SIDE_SURE = 0.8  # ... a side that needs no other evidence
SIDE_PART = 0.2  # ... and in every quarter of its length at least this (a line that stops halfway is none)
CHORD_SURE = 0.9  # a line right across a larger rectangle splits it only when it is this constant ...
CHORD_THIN = 0.22  # ... or cuts off no more than this share of it (a bar, a frame), else it is the room's
CHORD_MIN = 0.7  # ... and even a thin cut needs a line this good
SEG_MIN = 0.12  # every eighth of a side must be an edge at least this much
CHORD_CUT, CHORD_DEEP = 0.5, 0.35  # a line across the rectangle, this steady, within this share of a side
SIDE_JUMP = 0.25  # ... is the cam's border when the sides that meet it change this much there
STILL_SD = 2.0  # nine tenths of a strip change less than this over the scene: nothing filmed moves there
STILL_OF_CAM = 0.15  # ... or than this share of how much the rectangle changes (lettering is not noise-free)
DRAWN_SHARE = 0.2  # ... and this share of it lies on steady edges: lettering, a logo
STILL_MIN, STILL_MOST = 8, 0.2  # such a strip: at least this many pixels deep, at most this share of the side
STILL_LINE = 0.3  # the line it ends at: an edge in this share of (frames x its length)
FRAME_LINE = 0.45  # a decorative frame: lines this steady just inside three sides or more ...
FRAME_PAIR = 0.7  # ... or this steady inside two (a cam in a corner of the frame has only two to show)
STRIP_BUSY = 2.0  # ... or inside one, when the strip behind it changes this much more than the rest
MOVES_LIKE, MOVES_MIN = 0.4, 5.0  # a part moving this much of what moves around the cam is not the cam
LIVE_SD = 4.0  # a face that changes less than this over the scene is a picture of a face (HUD portrait)
GAME_SD = 8.0  # half the picture changes at least this much over the scene: a game, a video, a walk
ROOM_SD = 4.0  # half the picture changes less than this: a camera in a room, a desktop
ROOM_ALIKE = (0.6, 2.0)  # ... and a rectangle in it that lives this much of its surroundings is the room's
ROOM_SIDE = 0.75  # ... when one of its sides is an edge in less than this share
CAM_MAX_AREA = 0.55  # an "overlay" larger than this share of the frame is the picture itself (full camera)
SCENE_FRAMES = 24
CAM_MIN_W, CAM_MIN_H = 0.045, 0.05  # the smallest webcam looked for, as shares of the frame
MAX_LINES = 90


@dataclass
class SceneMaps:
    """What stays put over the frames of a scene. pv[y, b]: share of frames with a vertical edge on the
    boundary left of pixel column b (b = 0 and b = W are the frame's own sides); ph[b, x] the same for the
    boundary above pixel row b; sd[y, x]: how much the pixel changes over time (std of luma)."""

    pv: object
    ph: object
    sd: object
    n: int
    active: Rect | None = None  # the picture without black bars that stay (a cutscene's letterbox)

    @property
    def size(self) -> tuple[int, int]:
        return self.sd.shape[1], self.sd.shape[0]  # type: ignore[attr-defined]


def scene_maps(frames: list[Path], tau: float = EDGE_TAU) -> SceneMaps | None:
    """Edge persistence and temporal spread over frames of one scene (>= 4 frames, numpy needed)."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    pv = ph = s1 = s2 = None
    n = 0
    for p in frames:
        with Image.open(p) as im:
            a = np.asarray(im.convert("L"), dtype=np.float32)
        h, w = a.shape
        if s1 is None:
            pv, ph = np.zeros((h, w + 1), np.float32), np.zeros((h + 1, w), np.float32)
            s1, s2 = np.zeros((h, w), np.float64), np.zeros((h, w), np.float64)
        elif a.shape != s1.shape:
            continue
        # an edge on a boundary = a step in both 2 px differences that straddle it (one frame's blur or
        # ringing next to an edge does not count as a second edge)
        gx = np.abs(a[:, 2:] - a[:, :-2])
        pv[:, 2 : w - 1] += np.minimum(gx[:, :-1], gx[:, 1:]) > tau
        gy = np.abs(a[2:, :] - a[:-2, :])
        ph[2 : h - 1, :] += np.minimum(gy[:-1, :], gy[1:, :]) > tau
        s1 += a
        s2 += a * a
        n += 1
    if n < 4:
        return None
    sd = np.sqrt(np.maximum(s2 / n - (s1 / n) ** 2, 0.0)).astype(np.float32)
    return SceneMaps(pv / n, ph / n, sd, n, _active_area(s1 / n, sd))


def _active_area(mean, sd) -> Rect:
    """The frame without rows / columns at its sides that are black in every frame (median along the
    row, so a chat line or a logo drawn over a bar does not hide it). Bars of 8 px to a quarter of the
    frame; anything else is picture."""
    import numpy as np

    h, w = mean.shape

    def bars(dark) -> tuple[int, int]:
        lead = int(np.argmin(dark)) if not dark.all() else 0
        trail = int(np.argmin(dark[::-1])) if not dark.all() else 0
        size = len(dark)
        return (lead if 8 <= lead <= size // 4 else 0), (trail if 8 <= trail <= size // 4 else 0)

    top, bot = bars((np.median(mean, axis=1) < 8) & (np.median(sd, axis=1) < 2))
    left, right = bars((np.median(mean, axis=0) < 8) & (np.median(sd, axis=0) < 2))
    return left, top, w - left - right, h - top - bot


@dataclass
class OverlayRect:
    rect: Rect
    cov: tuple[float, float, float, float]  # left, right, top, bottom: share of the side that is an edge
    sure: int  # sides that are a frame side, a near-constant edge, or have the game beyond them
    still: float  # change over time inside / in a ring around it (a webcam's room is stiller than a game)
    weak: float = 1.0  # the least supported eighth of any side that is not the frame's

    @property
    def score(self) -> float:
        return min(self.cov)


def _peaks(v, floor: float, radius: int = 3):
    """Indices of local maxima of v that reach floor (one per radius)."""
    import numpy as np

    m = v >= floor
    for k in range(1, radius + 1):
        m &= (v >= np.roll(v, k)) & (v > np.roll(v, -k))
    return np.flatnonzero(m)


def overlay_rects(maps: SceneMaps) -> list[OverlayRect]:
    """Every rectangle of the scene whose four sides are edges that stay put (sides of the frame count).

    Webcam overlays are among them, so are HUD panels, bars, alerts and rectangles made of lines of the
    streamer's room; cam_rect and cam_proposals choose. Computed once per scene (kept on maps)."""
    import numpy as np

    cached = getattr(maps, "_rects", None)
    if cached is not None:
        return cached
    pv, ph, sd = maps.pv, maps.ph, maps.sd
    fw, fh = maps.size
    # dilate by one boundary: a soft border sits on one of two neighbouring boundaries from row to row
    pv1 = np.maximum(pv, np.maximum(np.roll(pv, 1, axis=1), np.roll(pv, -1, axis=1)))
    ph1 = np.maximum(ph, np.maximum(np.roll(ph, 1, axis=0), np.roll(ph, -1, axis=0)))
    cv = np.vstack([np.zeros((1, fw + 1), np.float32), np.cumsum(pv1, axis=0, dtype=np.float32)])
    chz = np.hstack([np.zeros((fh + 1, 1), np.float32), np.cumsum(ph1, axis=1, dtype=np.float32)])
    ii = np.pad(np.cumsum(np.cumsum(sd, axis=0, dtype=np.float64), axis=1), ((1, 0), (1, 0)))
    min_w, min_h = max(16, int(CAM_MIN_W * fw)), max(16, int(CAM_MIN_H * fh))

    def area_mean(x0: float, y0: float, x1: float, y1: float) -> float | None:
        x0, x1 = int(min(max(x0, 0), fw)), int(min(max(x1, 0), fw))
        y0, y1 = int(min(max(y0, 0), fh)), int(min(max(y1, 0), fh))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None
        return float(ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]) / ((x1 - x0) * (y1 - y0))

    # columns that hold an edge over at least a small cam's height
    run = (cv[min_h:] - cv[:-min_h]).max(axis=0) / min_h
    cols = [int(b) for b in _peaks(run, SIDE_MIN) if 2 <= b <= fw - 2]
    cols = sorted(sorted(cols, key=lambda b: -run[b])[:MAX_LINES])
    cols = [0, *cols, fw]
    out: list[OverlayRect] = []
    pad = 5  # strips start this far from the line: the border's own blend is neither side
    for i, left in enumerate(cols):
        for right in cols[i + 1 :]:
            cw = right - left
            if cw < min_w:
                continue
            hc = (chz[:, right] - chz[:, left]) / cw  # every horizontal boundary over [left, right)
            xq = [left + (cw * k) // 4 for k in range(5)]
            part = np.min(
                [(chz[:, xq[k + 1]] - chz[:, xq[k]]) / max(1, xq[k + 1] - xq[k]) for k in range(4)], axis=0
            )
            inner = _peaks(hc[1:fh], SIDE_MIN) + 1
            rows = np.concatenate([[0], inner[part[inner] >= SIDE_PART], [fh]])
            if len(rows) > MAX_LINES:
                inner = rows[1:-1]
                rows = np.concatenate([[0], np.sort(inner[np.argsort(-hc[inner])[: MAX_LINES - 2]]), [fh]])
            tt, bb = np.meshgrid(rows, rows, indexing="ij")
            ch = bb - tt
            ok = (ch >= min_h) & (cw >= 0.45 * ch) & (cw <= 2.8 * ch)
            if not ok.any():
                continue
            chs = np.maximum(ch, 1)
            lc = np.ones_like(chs, np.float32) if left == 0 else (cv[bb, left] - cv[tt, left]) / chs
            rc = np.ones_like(chs, np.float32) if right == fw else (cv[bb, right] - cv[tt, right]) / chs
            ok &= (lc >= SIDE_MIN) & (rc >= SIDE_MIN)
            for line in (left, right):  # no empty quarter on the vertical sides either
                if line in (0, fw):
                    continue
                for k in range(4):
                    y0, y1 = tt + (ch * k) // 4, tt + (ch * (k + 1)) // 4
                    ok &= (cv[y1, line] - cv[y0, line]) >= SIDE_PART * np.maximum(y1 - y0, 1)
            for a, b in zip(*np.nonzero(ok), strict=True):
                top, bot = int(tt[a, b]), int(bb[a, b])
                h = bot - top
                cov = (
                    float(lc[a, b]), float(rc[a, b]),
                    1.0 if top == 0 else float(hc[top]), 1.0 if bot == fh else float(hc[bot]),
                )  # fmt: skip
                depth = max(8, int(0.08 * min(cw, h)))
                strips = (  # (frame side?, coverage, inside strip, outside strip) per side
                    (left == 0, cov[0], (left + pad, top, left + pad + depth, bot),
                     (left - pad - depth, top, left - pad, bot)),
                    (right == fw, cov[1], (right - pad - depth, top, right - pad, bot),
                     (right + pad, top, right + pad + depth, bot)),
                    (top == 0, cov[2], (left, top + pad, right, top + pad + depth),
                     (left, top - pad - depth, right, top - pad)),
                    (bot == fh, cov[3], (left, bot - pad - depth, right, bot - pad),
                     (left, bot + pad, right, bot + pad + depth)),
                )  # fmt: skip
                sure = 0
                for at_frame, c, inside, outside in strips:
                    if at_frame or c >= SIDE_SURE:
                        sure += 1
                        continue
                    i_, o_ = area_mean(*inside), area_mean(*outside)
                    if i_ is not None and o_ is not None and max(i_, o_) >= 1.8 * min(i_, o_) + 3.0:
                        sure += 1  # two pictures: one of them moves far more than the other
                ring = max(12, int(0.12 * min(cw, h)))
                whole = area_mean(left, top, right, bot) or 0.0
                big = area_mean(left - ring, top - ring, right + ring, bot + ring) or 0.0
                a_in = cw * h
                a_big = (min(fw, right + ring) - max(0, left - ring)) * (
                    min(fh, bot + ring) - max(0, top - ring)
                )
                around = (big * a_big - whole * a_in) / max(1, a_big - a_in)
                weak = 1.0
                for k in range(8):
                    y0, y1 = top + (h * k) // 8, top + (h * (k + 1)) // 8
                    x0, x1 = left + (cw * k) // 8, left + (cw * (k + 1)) // 8
                    if left != 0:
                        weak = min(weak, float(cv[y1, left] - cv[y0, left]) / max(1, y1 - y0))
                    if right != fw:
                        weak = min(weak, float(cv[y1, right] - cv[y0, right]) / max(1, y1 - y0))
                    if top != 0:
                        weak = min(weak, float(chz[top, x1] - chz[top, x0]) / max(1, x1 - x0))
                    if bot != fh:
                        weak = min(weak, float(chz[bot, x1] - chz[bot, x0]) / max(1, x1 - x0))
                out.append(OverlayRect((left, top, cw, h), cov, sure, whole / max(around, 0.5), weak))
    maps._rects = out  # type: ignore[attr-defined]
    maps._lines = (cv, chz)  # type: ignore[attr-defined]
    return out


def cam_rect(maps: SceneMaps, face: Box) -> tuple[Rect, float] | None:
    """The overlay rectangle around a face: (x, y, w, h) of the webcam picture and how sure, or None.
    Sure is above 0.5 when all four sides are sure, up to 0.5 when one of them is only a line.

    Each side is an edge in most frames along the whole side and is either that in nearly all of them (a
    graphic border, another cam, a bar), or the pictures on its two sides change very differently over
    time (webcam and game). Of the rectangles like that around the face the smallest is the picture
    (inside a decorative frame, without the bars attached to it), unless what makes it smaller is a line
    of the room: a line that is no graphic edge and cuts a good part off a larger rectangle (the edge of
    a wall behind the streamer). The frame's own sides count, so a camera that is the whole picture comes
    back as (nearly) the whole frame: see is_overlay.
    """
    x, y, w, h = face
    around: list[OverlayRect] = []
    for r in overlay_rects(maps):
        left, top, cw, ch = r.rect
        right, bot = left + cw, top + ch
        if left > x + 0.1 * w or right < x + 0.9 * w or top > y + 0.1 * h or bot < y + 0.9 * h:
            continue  # the face is not inside (a chin or a forehead may be cut by the cam itself)
        if cw < 1.2 * w or ch < 1.2 * h:
            continue
        if left < x - 9 * w or right > x + 10 * w or top < y - 6 * h or bot > y + 9 * h:
            continue  # far too large for this face
        if r.weak < SEG_MIN:
            continue  # a side with a stretch of no edge at all: the rectangle reaches past the cam there
        around.append(r)
    if not around:
        return None
    best = max(r.sure for r in around)
    if best < 3:
        return None
    sure = sorted((r for r in around if r.sure == best), key=lambda r: r.rect[2] * r.rect[3])
    pick = sure[0]
    grown = True
    while grown:
        grown = False
        for big in sure:
            if big is not pick and _room_line_inside(maps, pick, big):
                pick, grown = big, True
                break
    rect = _tighten(maps, _trim_still(maps, _trim_moving(maps, pick.rect, face), face), face)
    return rect, (0.5 + 0.5 * pick.score if pick.sure == 4 else 0.5 * pick.score)


def _chords(maps: SceneMaps, rect: Rect, face: Box) -> dict[str, tuple[float, int]]:
    """For each side (l, r, t, b) the steadiest line that runs right across rect between that side and
    the face: (share of its length that is an edge, where it is: a column or a row of the picture)."""
    import numpy as np

    cv, chz = maps._lines  # type: ignore[attr-defined]
    x, y, w, h = rect
    fx, fy, fwd, fht = face
    rows = (chz[:, x + w] - chz[:, x]) / w
    cols = (cv[y + h, :] - cv[y, :]) / h

    def best(cov, lo: float, hi: float) -> tuple[float, int]:
        lo, hi = max(int(lo), 0), min(int(hi), len(cov))
        if hi - lo < 1:
            return 0.0, -1
        k = lo + int(np.argmax(cov[lo:hi]))
        return float(cov[k]), k

    edge = 4  # the side's own line is not a chord
    return {
        "t": best(rows, y + edge, fy - 0.15 * fht),
        "b": best(rows, fy + 1.15 * fht, y + h - edge + 1),
        "l": best(cols, x + edge, fx - 0.15 * fwd),
        "r": best(cols, fx + 1.15 * fwd, x + w - edge + 1),
    }


def _side_jump(maps: SceneMaps, rect: Rect, side: str, at: int) -> float | None:
    """How much the two sides that run into `side` change at the line `at`: the largest difference between
    their edge share beyond the line and before it. None when neither can tell (frame sides, a strip of
    a few pixels). A bar, a banner or a second camera attached to the webcam has borders of its own; the
    webcam's border does not change where a shelf of the room meets it."""
    cv, chz = maps._lines  # type: ignore[attr-defined]
    fw, fh = maps.size
    x, y, w, h = rect
    out: float | None = None
    if side in "tb":
        a0, a1, b0, b1 = (y, at, at, y + h) if side == "t" else (at, y + h, y, at)
        if a1 - a0 < 12 or b1 - b0 < 12:
            return None
        for line in (x, x + w):
            if line in (0, fw):
                continue
            d = abs(
                float(cv[a1, line] - cv[a0, line]) / (a1 - a0)
                - float(cv[b1, line] - cv[b0, line]) / (b1 - b0)
            )
            out = d if out is None else max(out, d)
    else:
        a0, a1, b0, b1 = (x, at, at, x + w) if side == "l" else (at, x + w, x, at)
        if a1 - a0 < 12 or b1 - b0 < 12:
            return None
        for line in (y, y + h):
            if line in (0, fh):
                continue
            d = abs(
                float(chz[line, a1] - chz[line, a0]) / (a1 - a0)
                - float(chz[line, b1] - chz[line, b0]) / (b1 - b0)
            )
            out = d if out is None else max(out, d)
    return out


def _cut(rect: Rect, side: str, at: int) -> Rect:
    x, y, w, h = rect
    if side == "t":
        return x, at, w, y + h - at
    if side == "b":
        return x, y, w, at - y
    if side == "l":
        return at, y, x + w - at, h
    return x, y, at - x, h


def _strip_ratio(maps: SceneMaps, rect: Rect, side: str, at: int) -> float:
    """How much the strip of rect between `side` and the line `at` changes over time, against the rest."""
    import numpy as np

    x, y, w, h = rect
    sd = maps.sd
    part = {
        "t": sd[y:at, x : x + w], "b": sd[at : y + h, x : x + w],
        "l": sd[y : y + h, x:at], "r": sd[y : y + h, at : x + w],
    }[side]  # type: ignore[index]  # fmt: skip
    rx, ry, rw, rh = _cut(rect, side, at)
    rest = sd[ry : ry + rh, rx : rx + rw]  # type: ignore[index]
    if not part.size or not rest.size:
        return 0.0
    return float(np.median(part)) / max(float(np.median(rest)), 0.5)


def _tighten(maps: SceneMaps, rect: Rect, face: Box) -> Rect:
    """rect without what does not belong to the webcam picture although steady edges enclose it:
    - a decorative frame: lines a few pixels inside three sides or more are the picture's edges (inside
      two when both are strong lines);
    - a label or counter drawn along one side: a line a few pixels inside it, behind which the strip
      changes far more than the webcam does (aidanbeans: 8 px of sub-goal text over the cam's top).
      Peeled last: the new side must not set off another cut;
    - a bar, a sponsor block, a second camera attached to the cam: cut at a line right across the rectangle
      (within CHORD_DEEP of a side) when that line is as constant as drawn graphics, or when the
      rectangle's own sides change there (a bar has borders of its own). Deeper than that the same
      signs are the edge of a wall as often as a sponsor panel (infume, 247loltyler1, pobelter against
      genburten): no cut, layout.doubts() says so.
    A line of the room (a shelf, the edge of a wall) is neither and stays inside. Tried and dropped on the
    labelled moments: peeling single lines near a side and cutting at drawn lines however deep (more
    panels too tight than bars removed). The rectangle only gets smaller here, never smaller than the
    face needs."""
    overlay_rects(maps)
    fwd, fht = face[2], face[3]

    def thin(rect: Rect) -> dict[str, int]:
        """Per side, a steady line a few pixels inside it (None for the others)."""
        x, y, w, h = rect
        at_side = {"t": y, "b": y + h, "l": x, "r": x + w}
        out = {}
        for side, (cov, at) in _chords(maps, rect, face).items():
            size = h if side in "tb" else w
            if at >= 0 and cov >= FRAME_LINE and abs(at - at_side[side]) <= max(14, 0.05 * size):
                out[side] = at
        return out

    def cut_all(rect: Rect, todo: dict[str, int]) -> Rect:
        for side, at in todo.items():
            cut = _cut(rect, side, at)
            if cut[2] >= 1.2 * fwd and cut[3] >= 1.2 * fht:
                rect = cut
        return rect

    for _ in range(6):
        x, y, w, h = rect
        at_side = {"t": y, "b": y + h, "l": x, "r": x + w}
        chords = _chords(maps, rect, face)
        todo = thin(rect)
        pair = [k for k in todo if chords[k][0] >= FRAME_PAIR]
        if len(todo) < 3 and len(pair) >= 2:
            todo = {k: todo[k] for k in pair}
        elif len(todo) < 3:
            todo = {}
            for side, (cov, at) in sorted(chords.items(), key=lambda kv: -kv[1][0]):
                size = h if side in "tb" else w
                if at < 0 or cov < CHORD_CUT or abs(at - at_side[side]) > CHORD_DEEP * size:
                    continue
                jump = _side_jump(maps, rect, side, at)
                if cov >= CHORD_SURE or (jump is not None and jump >= SIDE_JUMP):
                    todo = {side: at}
                    break
        new = cut_all(rect, todo)
        if new == rect:
            break
        rect = new
    # Last, and with nothing cut after it: a strip of lettering along one side.
    return cut_all(
        rect, {k: at for k, at in thin(rect).items() if _strip_ratio(maps, rect, k, at) >= STRIP_BUSY}
    )


def rect_in_frames(frames: list[Path], rect: Rect, tau: float = EDGE_TAU) -> list[float]:
    """For each frame the share of rect's border (its sides that are not the frame's) that is an edge in
    that one frame. Over a stretch in which the streamer switches layout the numbers fall to what any
    line of the picture gets by chance: the webcam is not there in those frames."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return []
    x, y, w, h = rect
    out = []
    for p in frames:
        with Image.open(p) as im:
            a = np.asarray(im.convert("L"), dtype=np.float32)
        fh, fw = a.shape
        parts = []
        for b in (x, x + w):
            if 2 <= b <= fw - 2:
                parts.append(np.abs(a[y : y + h, b] - a[y : y + h, b - 1]) > tau)
                parts[-1] |= np.abs(a[y : y + h, min(b + 1, fw - 1)] - a[y : y + h, b - 2]) > 2 * tau
        for b in (y, y + h):
            if 2 <= b <= fh - 2:
                parts.append(np.abs(a[b, x : x + w] - a[b - 1, x : x + w]) > tau)
                parts[-1] |= np.abs(a[min(b + 1, fh - 1), x : x + w] - a[b - 2, x : x + w]) > 2 * tau
        out.append(float(np.concatenate(parts).mean()) if parts else 1.0)
    return out


def _around(maps: SceneMaps, rect: Rect) -> float:
    """How much the picture just outside rect changes over time (median of a ring around it)."""
    import numpy as np

    x, y, w, h = rect
    d = max(16, int(0.15 * min(w, h)))
    sd = maps.sd
    ring = [
        sd[max(0, y - d) : y, max(0, x - d) : x + w + d],  # type: ignore[index]
        sd[y + h : y + h + d, max(0, x - d) : x + w + d],  # type: ignore[index]
        sd[y : y + h, max(0, x - d) : x],  # type: ignore[index]
        sd[y : y + h, x + w : x + w + d],  # type: ignore[index]
    ]
    ring = [r.ravel() for r in ring if r.size]
    return float(np.median(np.concatenate(ring))) if ring else 0.0


def _room_line_inside(maps: SceneMaps, small: OverlayRect, big: OverlayRect, tol: int = 3) -> bool:
    """big holds small, and every side of small that lies inside big is a line of the room rather than
    the cam's border: it is no graphic edge (not constant enough), what lies past it does not move like
    the picture around the cam (a chat or a sponsor panel running under it does), and it either cuts a
    good part off big (the edge of a wall) or is a poor line (a thin strip behind a good line is a bar or
    a frame and stays out)."""
    import numpy as np

    sx, sy, sw, sh = small.rect
    bx, by, bw, bh = big.rect
    if sx < bx - tol or sy < by - tol or sx + sw > bx + bw + tol or sy + sh > by + bh + tol:
        return False
    cut = (  # per side: px it lies inside big, that as a share of big, small's coverage, the part cut off
        (sx - bx, (sx - bx) / bw, small.cov[0], (bx, by, sx, by + bh)),
        (bx + bw - sx - sw, (bx + bw - sx - sw) / bw, small.cov[1], (sx + sw, by, bx + bw, by + bh)),
        (sy - by, (sy - by) / bh, small.cov[2], (bx, by, bx + bw, sy)),
        (by + bh - sy - sh, (by + bh - sy - sh) / bh, small.cov[3], (bx, sy + sh, bx + bw, by + bh)),
    )
    inner = [c for c in cut if c[0] > tol]
    if not inner:
        return False
    beyond = _around(maps, big.rect)
    for _px, share, cov, (x0, y0, x1, y1) in inner:
        if cov >= CHORD_SURE:
            return False
        part = maps.sd[max(0, y0) : y1, max(0, x0) : x1]  # type: ignore[index]
        if part.size and beyond >= MOVES_MIN and float(np.median(part)) >= MOVES_LIKE * beyond:
            return False  # what lies past this line moves like the picture around the cam
        if share <= CHORD_THIN and cov >= CHORD_MIN:
            return False  # a bar, a frame
    return True


def _trim_moving(maps: SceneMaps, rect: Rect, face: Box, most: float = 0.3) -> Rect:
    """rect without the strips along its sides that move like the picture around it and far more than
    the cam does: an info bar or alert drawn under the cam with no visible edge against a dark shirt.
    Never past the face, never more than most of a side, never a side that is the frame's own."""
    import numpy as np

    fw, fh = maps.size
    x, y, w, h = rect
    beyond = _around(maps, rect)
    sd = maps.sd[y : y + h, x : x + w]  # type: ignore[index]
    calm = float(np.median(sd))
    if beyond < MOVES_MIN or calm * 6 > beyond:
        return rect  # nothing moves around the cam, or the cam moves about as much itself
    rows, cols = np.median(sd, axis=1), np.median(sd, axis=0)
    floor = max(MOVES_LIKE * beyond, 6 * calm)

    def run(v) -> int:
        n = 0
        while n < len(v) and v[n] >= floor:
            n += 1
        return n

    fx, fy, fw_, fh_ = face
    top = min(run(rows), int(most * h), max(0, int(fy - y))) if y > 0 else 0
    bot = min(run(rows[::-1]), int(most * h), max(0, int(y + h - fy - fh_))) if y + h < fh else 0
    left = min(run(cols), int(most * w), max(0, int(fx - x))) if x > 0 else 0
    right = min(run(cols[::-1]), int(most * w), max(0, int(x + w - fx - fw_))) if x + w < fw else 0
    top, bot, left, right = (v if v >= 6 else 0 for v in (top, bot, left, right))
    return x + left, y + top, w - left - right, h - top - bot


def _trim_still(maps: SceneMaps, rect: Rect, face: Box) -> Rect:
    """rect without a banner attached to the webcam: a strip along a side in which nothing ever moves,
    that is full of steady edges (lettering, a logo) and ends at a steady line right across the
    rectangle (arrowcs: 32 px of 'use code' under the picture, inside the rectangle's sides). Stillness
    alone does not say it: the dark wall behind a streamer is as still, and tried without the lettering
    this cut into 40 real cams of 250. Never past the face, never more than STILL_MOST of a side."""
    import numpy as np

    overlay_rects(maps)
    cv, chz = maps._lines  # type: ignore[attr-defined]
    x, y, w, h = rect
    fx, fy, fwd, fht = (int(v) for v in face)
    sd = maps.sd[y : y + h, x : x + w]  # type: ignore[index]
    if sd.shape[0] < 16 or sd.shape[1] < 16:
        return rect
    drawn = (maps.pv[y : y + h, x : x + w] >= 0.8) | (maps.ph[y : y + h, x : x + w] >= 0.8)  # type: ignore[index]
    floor = max(STILL_SD, STILL_OF_CAM * float(np.median(sd)))
    rows = np.percentile(sd, 90, axis=1) <= floor
    cols = np.percentile(sd, 90, axis=0) <= floor
    row_line = (chz[y : y + h + 1, x + w] - chz[y : y + h + 1, x]) / w  # boundary i: above row y + i
    col_line = (cv[y + h, x : x + w + 1] - cv[y, x : x + w + 1]) / h

    def depth(still, line, lettering, size: int, room: float) -> int:
        skip = 0
        while skip < 4 and skip < len(still) and not still[skip]:
            skip += 1
        n = skip
        while n < len(still) and still[n]:
            n += 1
        lo, hi = max(1, n - 2), min(len(line) - 1, n + 4)
        if n - skip < STILL_MIN or hi <= lo:
            return 0
        k = lo + int(np.argmax(line[lo:hi]))
        if line[k] < STILL_LINE or k > min(STILL_MOST * size, room):
            return 0
        return k if float(lettering[:k].mean()) >= DRAWN_SHARE else 0

    top = depth(rows, row_line, drawn, h, fy - y)
    bot = depth(rows[::-1], row_line[::-1], drawn[::-1], h, y + h - fy - fht)
    left = depth(cols, col_line, drawn.T, w, fx - x)
    right = depth(cols[::-1], col_line[::-1], drawn.T[::-1], w, x + w - fx - fwd)
    new = (x + left, y + top, w - left - right, h - top - bot)
    if new[2] < 1.2 * fwd or new[3] < 1.2 * fht:
        return rect
    cut = {("l", x + left): left, ("r", x + w - right): right, ("t", y + top): top, ("b", y + h - bot): bot}
    banners = getattr(maps, "_banners", None) or set()
    maps._banners = banners | {k for k, v in cut.items() if v}  # type: ignore[attr-defined]
    return new


def banner_sides(maps: SceneMaps, rect: Rect) -> list[str]:
    """Sides of rect (l, r, t, b) that _trim_still put where a banner begins. Such a side is often a
    faint line (a dark shirt against a dark banner), but what lies behind it says where the picture ends."""
    x, y, w, h = rect
    at = {"l": x, "r": x + w, "t": y, "b": y + h}
    return [k for k in "lrtb" if (k, at[k]) in (getattr(maps, "_banners", None) or set())]


def cam_evidence(maps: SceneMaps, rect: Rect, face: Box) -> dict:
    """What can be measured about a webcam rectangle found around a face. No verdict here: layout decides
    from these whether the rectangle is surely the webcam and nothing else.

    sides: share of each side (l, r, t, b) that is a steady edge (1.0 = a side of the frame);
    beyond: how the picture just outside each side changes over time against just inside it (None at the
    frame's sides); chords: for each side the steadiest line that runs right across the rectangle between
    that side and the face, as (share of its length that is an edge, its distance from the side as a
    share of the rectangle, how much the rectangle's sides change there): a bar, a banner or a second
    camera attached to the cam ends at such a line, and so does a shelf in the room; strips: how much
    the strip behind that line changes over time against the rest of the rectangle (hands on a desk and
    a second camera move far more than the room); banner: sides put where a banner begins; outer /
    inner: other rectangles of the scene that hold this one, or lie in it around the face, as area
    ratios (a cam inside a player window, a frame around the cam)."""
    import numpy as np

    overlay_rects(maps)
    cv, chz = maps._lines  # type: ignore[attr-defined]
    sd = maps.sd
    fw, fh = maps.size
    x, y, w, h = rect
    fx, fy, fwd, fht = face

    def strip(x0: float, y0: float, x1: float, y1: float) -> float | None:
        part = sd[max(0, int(y0)) : max(0, int(y1)), max(0, int(x0)) : max(0, int(x1))]  # type: ignore[index]
        return float(np.median(part)) if part.size else None

    sides = {
        "l": 1.0 if x == 0 else float(cv[y + h, x] - cv[y, x]) / h,
        "r": 1.0 if x + w == fw else float(cv[y + h, x + w] - cv[y, x + w]) / h,
        "t": 1.0 if y == 0 else float(chz[y, x + w] - chz[y, x]) / w,
        "b": 1.0 if y + h == fh else float(chz[y + h, x + w] - chz[y + h, x]) / w,
    }
    d, pad = max(8, int(0.08 * min(w, h))), 5
    pairs = {
        "l": ((x + pad, y, x + pad + d, y + h), (x - pad - d, y, x - pad, y + h), x == 0),
        "r": (
            (x + w - pad - d, y, x + w - pad, y + h),
            (x + w + pad, y, x + w + pad + d, y + h),
            x + w == fw,
        ),
        "t": ((x, y + pad, x + w, y + pad + d), (x, y - pad - d, x + w, y - pad), y == 0),
        "b": (
            (x, y + h - pad - d, x + w, y + h - pad),
            (x, y + h + pad, x + w, y + h + pad + d),
            y + h == fh,
        ),
    }
    beyond = {}
    for k, (inside, outside, at_frame) in pairs.items():
        i, o = strip(*inside), strip(*outside)
        beyond[k] = None if at_frame or i is None or o is None else (round(i, 2), round(o, 2))

    chords, strips = {}, {}
    for side, (c, at) in _chords(maps, rect, face).items():
        size = h if side in "tb" else w
        depth = abs(at - {"t": y, "b": y + h, "l": x, "r": x + w}[side]) if at >= 0 else 0
        jump = _side_jump(maps, rect, side, at) if at >= 0 else None
        chords[side] = (round(c, 3), round(depth / size, 3), None if jump is None else round(jump, 2))
        if at < 0:
            continue
        rx, ry, rw, rh = _cut(rect, side, at)  # the rectangle without the strip behind this line
        part = {
            "t": sd[y:at, x : x + w], "b": sd[at : y + h, x : x + w],
            "l": sd[y : y + h, x:at], "r": sd[y : y + h, at : x + w],
        }[side]  # type: ignore[index]  # fmt: skip
        rest = sd[ry : ry + rh, rx : rx + rw]  # type: ignore[index]
        if part.size and rest.size:
            strips[side] = round(float(np.median(part)) / max(float(np.median(rest)), 0.5), 2)
    banner = [  # only where the picture moves right up to the dead strip (k4sen: a minimap's label)
        k for k in banner_sides(maps, rect) if beyond.get(k) and beyond[k][0] >= 1.8 * beyond[k][1] + 3.0
    ]
    area = w * h
    outer, inner = [], []
    for r in overlay_rects(maps):
        if r.sure < 3 or r.rect == rect:
            continue
        rx, ry, rw, rh = r.rect
        a = rw * rh / area
        if rx <= x + 3 and ry <= y + 3 and rx + rw >= x + w - 3 and ry + rh >= y + h - 3 and a > 1.04:
            if rw * rh <= CAM_MAX_AREA * fw * fh:
                outer.append((round(a, 2), r.sure, round(r.score, 2)))
        elif rx >= x - 3 and ry >= y - 3 and rx + rw <= x + w + 3 and ry + rh <= y + h + 3 and a < 0.96:
            holds_face = rx <= fx + 0.1 * fwd and rx + rw >= fx + 0.9 * fwd and ry <= fy + 0.1 * fht
            if holds_face and ry + rh >= fy + 0.9 * fht and rw >= 1.2 * fwd and rh >= 1.2 * fht:
                inner.append((round(a, 2), r.sure, round(r.score, 2)))
    inside_all = float(np.median(sd[y : y + h, x : x + w]))  # type: ignore[index]
    return {
        "rect": list(rect), "sides": {k: round(v, 3) for k, v in sides.items()}, "beyond": beyond,
        "chords": chords, "strips": strips, "banner": banner,
        "outer": sorted(outer)[:4], "inner": sorted(inner, reverse=True)[:4],
        "aspect": round(w / h, 3), "face_h": round(fht / h, 3),
        "face_at": [round((fx + fwd / 2 - x) / w, 3), round((fy + fht / 2 - y) / h, 3)],
        "live": round(inside_all / max(_around(maps, rect), 0.5), 3), "inside_sd": round(inside_all, 2),
        "scene_sd": round(float(np.median(sd)), 2),  # type: ignore[call-overload]
    }  # fmt: skip


def face_alive(maps: SceneMaps, face: Box) -> bool:
    """The face moves over the scene (a webcam, a video), unlike a portrait in a HUD or a drawing."""
    x, y, w, h = (int(v) for v in face)
    part = maps.sd[max(0, y) : y + h, max(0, x) : x + w]  # type: ignore[index]
    return bool(part.size) and float(part.mean()) >= LIVE_SD


def room_rectangle(maps: SceneMaps, rect: Rect) -> bool:
    """rect is a rectangle of the room a full-screen camera shows (a doorway, a window, the top of a
    lower third with a studio wall), not an overlay: the whole picture is as still as a room, the inside
    of the rectangle lives about as much as what is around it (a webcam over a desktop is camera noise
    against dead-still graphics, over a game it is the calm part), and one of its sides is a poor line
    (a doorway, the edge of a studio wall; an overlay's four sides are drawn, hasanabi's cam over a paused
    video: 0.8 and more, the cams of a talk show's grid: 0.93 and more)."""
    import numpy as np

    if float(np.median(maps.sd)) >= ROOM_SD:  # type: ignore[call-overload]
        return False
    x, y, w, h = rect
    inside = float(np.median(maps.sd[y : y + h, x : x + w]))  # type: ignore[index]
    ratio = inside / max(_around(maps, rect), 0.5)
    if not ROOM_ALIKE[0] < ratio < ROOM_ALIKE[1]:
        return False
    overlay_rects(maps)
    cv, chz = maps._lines  # type: ignore[attr-defined]
    fw, fh = maps.size
    # The rectangle's own sides, as it is now: one that was tightened or trimmed is in no list, and
    # counted as 'room' for that alone (pikabooirl's cam, a cam of grandpoobear's grid).
    cov = (
        float(cv[y + h, x] - cv[y, x]) / h, float(cv[y + h, x + w] - cv[y, x + w]) / h,
        float(chz[y, x + w] - chz[y, x]) / w, float(chz[y + h, x + w] - chz[y + h, x]) / w,
    )  # fmt: skip
    at_frame = (x == 0, x + w == fw, y == 0, y + h == fh)
    return any(c < ROOM_SIDE for c, e in zip(cov, at_frame, strict=True) if not e)


def scene_spread(maps: SceneMaps) -> float:
    """How much the middle pixel of the picture changes over the scene (median of the temporal spread)."""
    import numpy as np

    return float(np.median(maps.sd))  # type: ignore[call-overload]


def steady_share(maps: SceneMaps) -> float:
    """Share of the picture's pixels that lie on an edge in nearly every frame: drawn things (HUD, bars,
    a webcam's border) and whatever a camera that does not move shows."""

    pv, ph = maps.pv, maps.ph
    return float(((pv[:, :-1] >= 0.8) | (ph[:-1, :] >= 0.8)).mean())  # type: ignore[index]


def scene_still(maps: SceneMaps) -> bool:
    """Most of the picture stays as it is over the scene: a camera in a room, a desktop, a menu."""
    import numpy as np

    return float(np.median(maps.sd)) < GAME_SD  # type: ignore[call-overload]


def cutout_box(face: Box, fw: int, fh: int, aspect: float = 1080 / 960) -> Rect:
    """Crop for a webcam with no rectangle of its own (green-screen cut-out, or the judge's point): head
    and shoulders around the face in the panel's shape, kept inside the frame."""
    x, y, w, h = face
    ch = min(fh, 2.6 * h, fw / aspect)
    cw = ch * aspect
    left = min(max(0.0, x + w / 2 - cw / 2), fw - cw)
    top = min(max(0.0, y - 0.75 * h), fh - ch)
    return _even(left), _even(top), _even(cw), _even(ch)


# A layout switch: this share of what stood still changes at once, and of what stands still after is new
CUT_CHANGE = 0.7
CUT_KEPT = 0.5  # ... and less than this share of the edges that stay put (borders, HUD, the room) survives it
CUT_RUN = 6  # frames on each side those edges are read from


def _steady_edges(run: list):
    """Edges present in (nearly) all of a run of small grey frames (bool map)."""
    import numpy as np

    acc = np.zeros(run[0].shape, np.float32)
    for a in run:
        gx = np.abs(np.diff(a, axis=1, prepend=a[:, :1])) > 10
        gy = np.abs(np.diff(a, axis=0, prepend=a[:1, :])) > 10
        acc += gx | gy
    return acc >= max(2, len(run) - 1)


def same_scene(
    frames: list[tuple[float, Path]], at_s: float | None, min_frames: int = 4
) -> list[tuple[float, Path]]:
    """The frames that show the layout on screen at at_s (the middle of the frames when None).

    The streamer switches scene (camera full screen, a grid of cams, another corner): most of what stood
    still changes at once, and so does most of what stands still afterwards. A cut inside the game does
    that too (a still shot, a loading screen), so the edges that stay put before and after are compared:
    a webcam's border, its room, the HUD survive a cut of the game, not a change of layout. Frames are
    split at layout changes and the run around at_s is returned; all frames when nothing changes or that
    run would be too short to read a layout from."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return frames
    if len(frames) < 2 * min_frames:
        return frames
    small = []
    for _, p in frames:
        with Image.open(p) as im:
            small.append(np.asarray(im.convert("L").resize((480, 270)), dtype=np.int16))
    same = [np.abs(b - a) < 8 for a, b in zip(small, small[1:], strict=False)]
    cuts: list[int] = []
    for i in range(1, len(same) - 1):
        before, after = same[i - 1], same[i + 1]  # what stood still just before / just after this step
        if before.mean() < 0.02 or after.mean() < 0.02:
            continue
        if min(1.0 - float(same[i][before].mean()), 1.0 - float(same[i][after].mean())) <= CUT_CHANGE:
            continue
        lo = cuts[-1] if cuts else 0
        a, b = small[max(lo, i + 1 - CUT_RUN) : i + 1], small[i + 1 : i + 1 + CUT_RUN]
        if len(a) < 3 or len(b) < 3:
            continue
        ea, eb = _steady_edges(a), _steady_edges(b)
        both = float((ea & eb).sum())
        if ea.sum() < 50 or eb.sum() < 50:
            continue  # nothing stays put on one side: no layout to compare
        if max(both / ea.sum(), both / eb.sum()) < CUT_KEPT:
            cuts.append(i + 1)
    if not cuts:
        return frames
    t = at_s if at_s is not None else frames[len(frames) // 2][0]
    k = min(range(len(frames)), key=lambda i: abs(frames[i][0] - t))
    lo = max([c for c in cuts if c <= k], default=0)
    hi = min([c for c in cuts if c > k], default=len(frames))
    return frames[lo:hi] if hi - lo >= min_frames else frames


def cam_proposals(maps: SceneMaps, limit: int = 5) -> list[OverlayRect]:
    """Rectangles that look like a webcam overlay without having seen a face: four sure sides, a cam's
    size and shape, and stiller inside than the picture around them. Best first, overlapping ones merged.
    For looking closer (zoom_faces) where the full-frame face pass found nothing."""
    fw, fh = maps.size
    cands = [
        r
        for r in overlay_rects(maps)
        if r.sure == 4
        and 0.012 * fw * fh <= r.rect[2] * r.rect[3] <= 0.45 * fw * fh
        and 0.7 <= r.rect[2] / r.rect[3] <= 2.4
        and r.still <= 0.7
    ]
    cands.sort(key=lambda r: (-r.score, r.still))
    out: list[OverlayRect] = []
    for r in cands:
        if all(_rect_iou(r.rect, o.rect) < 0.5 for o in out):
            out.append(r)
        if len(out) >= limit:
            break
    return out


def _rect_iou(a: Rect, b: Rect) -> float:
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    union = a[2] * a[3] + b[2] * b[3] - ix * iy
    return ix * iy / union if union > 0 else 0.0


def zoom_faces(frames: list[Path], detector: FaceDetector, rect: Rect, out_h: int = 384) -> list[Box]:
    """Faces inside one rectangle of the picture, searched on an enlargement of it: a webcam's face is a
    few percent of the frame (missed by the full-frame pass when dark, small or turned away) but a good
    part of its own cam. One list entry per frame that shows a face (its largest), in source pixels."""
    from PIL import Image

    x, y, w, h = rect
    k = max(1.0, out_h / max(h, 1))
    boxes: list[Box] = []
    for i, f in enumerate(frames):
        with Image.open(f) as im:
            crop = im.convert("RGB").crop((x, y, x + w, y + h))
            crop = crop.resize((max(2, round(w * k)), max(2, round(h * k))), Image.LANCZOS)
            q = f.with_name(f"zoom_{i:02d}.png")
            crop.save(q, compress_level=1)
        found = [b for b in detector(q) if b[3] >= 0.1 * crop.height]
        if found:
            bx, by, bw, bh = max(found, key=lambda b: b[2] * b[3])
            boxes.append((x + bx / k, y + by / k, bw / k, bh / k))
    return boxes


def is_overlay(rect: Rect, fw: int, fh: int) -> bool:
    """A webcam rectangle that is a part of the picture, not (nearly) the picture itself."""
    return rect[2] * rect[3] <= CAM_MAX_AREA * fw * fh


def rect_present(maps: SceneMaps, rect: Rect, slack: int = 3) -> bool:
    """Is there an overlay with these sides in the scene? (a confirmed facecam_rect is only as good as
    the streamer's layout on the day it was confirmed)"""
    import numpy as np

    fw, fh = maps.size
    x, y, w, h = rect
    if w < 8 or h < 8 or x < 0 or y < 0 or x + w > fw or y + h > fh:
        return False

    def vline(b: int) -> float:
        if b <= slack or b >= fw - slack:
            return 1.0
        near = maps.pv[y : y + h, b - slack : b + slack + 1]  # type: ignore[index]
        return float(np.max(np.mean(near, axis=0)))

    def hline(b: int) -> float:
        if b <= slack or b >= fh - slack:
            return 1.0
        near = maps.ph[b - slack : b + slack + 1, x : x + w]  # type: ignore[index]
        return float(np.max(np.mean(near, axis=1)))

    return min(vline(x), vline(x + w), hline(y), hline(y + h)) >= SIDE_MIN


def scene_frames(
    media: Path, out_dir: Path, window: tuple[float, float] | None = None, n: int = SCENE_FRAMES
) -> list[tuple[float, Path]]:
    """Up to n full-size frames of the scene within window (s), from the stream's keyframes only: a live
    recording has one every 2 s and decoding just those takes a second where every frame takes ten. The
    times are approximate (position in the window); falls back to sample_frames when the file has too few
    keyframes there."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    times: list[float] = []
    try:
        dur = media_duration(media)
        lo, hi = (max(0.0, window[0]), min(dur, window[1])) if window else (0.0, dur)
        if hi - lo < 1.0:
            lo, hi = 0.0, dur
        t0 = seek.file_start(media) or 0.0
        vf = f"trim=start={t0 + lo:.3f}:end={t0 + hi:.3f},showinfo"
        cmd = [config.ffmpeg_bin(), "-y", "-hide_banner", "-nostats", "-skip_frame", "nokey", "-copyts",
               "-ss", f"{max(0.0, lo - 1.0):.3f}", "-i", str(media), "-an", "-fps_mode", "passthrough",
               "-vf", vf, str(out_dir / "key_%04d.bmp")]  # fmt: skip
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        files = sorted(out_dir.glob("key_*.bmp"))
        times = [float(m) - t0 for m in re.findall(r"showinfo.*?pts_time:\s*([0-9.]+)", r.stderr)]
    except (OSError, subprocess.SubprocessError, ValueError):
        files = []
    if len(files) < 6:
        return sample_frames(media, out_dir, min(n, 8), window=window)
    if len(times) != len(files):  # frame times unknown: spread over the window
        times = [lo + (i + 0.5) * (hi - lo) / len(files) for i in range(len(files))]
    pairs = list(zip(times, files, strict=True))
    if len(pairs) > n:
        step = len(pairs) / n
        pairs = [pairs[int(i * step)] for i in range(n)]
    return [(round(t, 2), p) for t, p in pairs]


def gameplay_crop(
    cams: list[Rect], fw: int, fh: int, aspect: float = 1080 / 960, active: Rect | None = None
) -> Rect:
    """The gameplay panel's source crop: as much of the game as the panel's shape allows, without the
    webcams. Full height, as near the centre as the cams allow. When every full-height window holds a
    cam, the largest window beside, above or below the cams if it still shows 60 % as much picture;
    otherwise the full-height window with the least cam in it (the cam then really overlaps the game).
    active = the picture inside the game's own black bars (SceneMaps.active): the crop stays inside."""
    if active and (active[2], active[3]) != (fw, fh) and active[2] >= 16 and active[3] >= 16:
        ax, ay, aw, ah = active
        inside = [(cx - ax, cy - ay, cw, ch) for cx, cy, cw, ch in cams]
        x, y, w, h = gameplay_crop(inside, aw, ah, aspect)
        return _even(x + ax), _even(y + ay), w, h
    w = _even(min(fw, fh * aspect))
    h = _even(min(fh, w / aspect))

    def cam_area(r: Rect) -> float:
        x, y, rw, rh = r
        return sum(
            max(0, min(x + rw, cx + cw) - max(x, cx)) * max(0, min(y + rh, cy + ch) - max(y, cy))
            for cx, cy, cw, ch in cams
        )

    y0 = _even((fh - h) / 2)
    mid = (fw - w) / 2
    xs = {mid, 0.0, float(fw - w)}
    for cx, _cy, cw, _ch in cams:
        xs.update({float(cx + cw), float(cx - w)})
    full = [(_even(min(max(x, 0), fw - w)), y0, w, h) for x in xs]
    best = min(full, key=lambda r: (cam_area(r), abs(r[0] - mid)))
    if cam_area(best) <= 0.005 * w * h:
        return best
    free: list[Rect] = []
    xs_ = sorted({0, fw, *(v for cx, _cy, cw, _ch in cams for v in (cx, cx + cw))})
    ys_ = sorted({0, fh, *(v for _cx, cy, _cw, ch in cams for v in (cy, cy + ch))})
    for i, rx in enumerate(xs_):  # every window bounded by the frame's and the cams' sides
        for rx1 in xs_[i + 1 :]:
            for j, ry in enumerate(ys_):
                for ry1 in ys_[j + 1 :]:
                    ex, ey = rx + rx % 2, ry + ry % 2  # even pixels inside the window
                    rw, rh = _even(rx1 - ex), _even(ry1 - ey)
                    if rw < 16 or rh < 16:
                        continue
                    ww = _even(min(rw, rh * aspect))
                    hh = _even(ww / aspect)
                    # as near the frame centre as the window allows
                    x = _even(min(max((fw - ww) / 2, ex), ex + rw - ww))
                    y = _even(min(max((fh - hh) / 2, ey), ey + rh - hh))
                    if cam_area((x, y, ww, hh)) == 0:
                        free.append((x, y, ww, hh))
    if free:
        r = max(free, key=lambda r: r[2] * r[3])
        if r[2] * r[3] >= 0.6 * w * h:
            return r
    return best


def draw_still(frame: Path, rects: Rects, out: Path) -> Path:
    from PIL import Image, ImageDraw

    im = Image.open(frame).convert("RGB")
    d = ImageDraw.Draw(im)
    lw = max(3, im.width // 300)
    for r, col, label in (
        (rects.facecam, (255, 40, 40), "facecam"),
        (rects.gameplay, (40, 220, 80), "gameplay"),
    ):
        if r:
            x, y, w, h = r
            d.rectangle([x, y, x + w - 1, y + h - 1], outline=col, width=lw)
            d.text((x + 8, y + 8), label, fill=col)
    if rects.face:
        x, y, w, h = rects.face
        d.rectangle([x, y, x + w, y + h], outline=(255, 255, 0), width=max(1, lw // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return out


def render_preview_9x16(media: Path, t: float, rects: Rects, streamer: str, out_dir: Path) -> Path | None:
    """Render a 1 s Short with the proposed rects and grab one frame of it. None if rendering fails."""
    try:
        from .render.template import render_short

        mp4 = render_short(
            media, t, 1.0, out_dir / "preview_9x16.mp4", clip_id=f"calib-{streamer}", streamer=streamer,
            words=[], hook="CALIBRATION", cam=rects.facecam, game=rects.gameplay,
            has_webcam=rects.facecam is not None, profile="cpu_live",
        )  # fmt: skip
        png = out_dir / "preview_9x16.png"
        subprocess.run(
            [config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.5", "-i", str(mp4),
             "-frames:v", "1", str(png)],
            check=True, capture_output=True,
        )  # fmt: skip
        return png
    except Exception as e:  # noqa: BLE001
        log.warning("9:16 preview failed: %s", e)
        return None


@dataclass
class Calibration:
    rects: Rects | None
    still: Path | None = None
    preview_9x16: Path | None = None
    message: str = ""


def calibrate(
    media: Path, streamer: str, *, detector: FaceDetector | None = None, out_dir: Path | None = None,
    n_frames: int = 10, render_preview: bool = True,
) -> Calibration:  # fmt: skip
    media = Path(media)
    out_dir = Path(out_dir) if out_dir else config.ROOT / "work" / "calibrate" / streamer
    detector = detector or make_detector()
    if detector is None:
        return Calibration(None, message="no face detector (install mediapipe or opencv-python-headless)")
    frames = sample_frames(media, out_dir, n_frames)
    if not frames:
        return Calibration(None, message="could not extract frames")
    boxes: list[Box] = []
    for _, p in frames:
        boxes.extend(detector(p))
    from PIL import Image

    with Image.open(frames[0][1]) as im:
        fw, fh = im.size
    rects = compute_rects(boxes, fw, fh, len(frames))
    mid_t, mid_frame = frames[len(frames) // 2]
    still = draw_still(mid_frame, rects, out_dir / "preview_still.png")
    prev = render_preview_9x16(media, mid_t, rects, streamer, out_dir) if render_preview else None
    msg = "ok" if rects.facecam else "no stable face found; facecam_rect left unset"
    return Calibration(rects, still, prev, msg)


# --------------------------------------------------------------------------- streamers.yaml update
def update_streamer_rects(path: Path, name: str, values: dict[str, str]) -> None:
    """Set keys inside one streamer block, editing text in place so comments and other keys survive."""
    lines = Path(path).read_text().splitlines()
    head = re.compile(rf"^(\s+){re.escape(name.lower())}:\s*(#.*)?$")
    start = next((i for i, ln in enumerate(lines) if head.match(ln)), None)
    if start is None:
        if not any(ln.startswith("streamers:") for ln in lines):
            lines.append("streamers:")
        lines.append(f"  {name.lower()}:")
        lines.append("    platform: twitch")
        for k, v in values.items():
            lines.append(f'    {k}: "{v}"')
    else:
        base = len(head.match(lines[start]).group(1))  # type: ignore[union-attr]
        end = start + 1
        while end < len(lines):
            ln = lines[end]
            if ln.strip() and not ln.lstrip().startswith("#") and len(ln) - len(ln.lstrip()) <= base:
                break
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        for k, v in values.items():
            pat = re.compile(rf"^\s+{k}:")
            idx = next((i for i in range(start + 1, end) if pat.match(lines[i])), None)
            new = " " * (base + 2) + f'{k}: "{v}"'
            if idx is None:
                lines.insert(end, new)
                end += 1
            else:
                lines[idx] = new
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(p)


def send_for_confirmation(still: Path, streamer: str, rects: Rects, token: str | None = None) -> bool:
    """Post the still to the operators' Telegram chat. False when the bot/chat is not configured."""
    import asyncio
    import os

    try:
        from .queue import telegram_bot

        token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = telegram_bot._chat_id()
        if not token or not chat_id:
            return False
        from telegram import Bot

        caption = (
            f"Calibration {streamer}\nfacecam_rect: {rect_str(rects.facecam) if rects.facecam else 'none'}\n"
            f"gameplay_rect: {rect_str(rects.gameplay)}\nConfirm, then rerun with --write."
        )

        async def go() -> None:
            async with Bot(token) as bot:
                with open(still, "rb") as f:
                    await bot.send_photo(chat_id, f, caption=caption)

        asyncio.run(go())
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("telegram confirmation failed: %s", e)
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipwatcher.calibrate")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--streamer", required=True)
    ap.add_argument("--frames", type=int, default=10)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--write", action="store_true", help="confirmed: update config/streamers.yaml")
    ap.add_argument("--telegram", action="store_true", help="send the still to Telegram for confirmation")
    a = ap.parse_args(argv)
    res = calibrate(a.input, a.streamer, out_dir=a.out, n_frames=a.frames)
    if res.rects is None:
        print(f"calibration unavailable: {res.message}", file=sys.stderr)
        return 2
    r = res.rects
    print(f"facecam_rect:   {rect_str(r.facecam) if r.facecam else 'none'}")
    print(f"gameplay_rect:  {rect_str(r.gameplay)}")
    print(f"still: {res.still}\n9:16 preview: {res.preview_9x16}\n{res.message}")
    if a.telegram and res.still:
        print("telegram:", "sent" if send_for_confirmation(res.still, a.streamer, r) else "not sent")
    if a.write:
        vals = {"gameplay_rect": rect_str(r.gameplay)}
        if r.facecam:
            vals["facecam_rect"] = rect_str(r.facecam)
        update_streamer_rects(config.CONFIG_DIR / "streamers.yaml", a.streamer, vals)
        print("streamers.yaml updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
