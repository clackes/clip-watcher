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


def sample_frames(media: Path, out_dir: Path, n: int = 10) -> list[tuple[float, Path]]:
    """n PNG frames spread over the middle 90% of the media (single-frame grabs, no trimming)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = media_duration(media)
    res = []
    for i in range(n):
        t = dur * (0.05 + 0.9 * (i + 0.5) / n)
        p = out_dir / f"frame_{i:02d}.png"
        subprocess.run(
            [config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
             "-i", str(media), "-frames:v", "1", str(p)],
            check=True, capture_output=True,
        )  # fmt: skip
        if p.exists():
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
        r = subprocess.run(
            [config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}",
             "-i", str(media), "-frames:v", "1", *(["-vf", f"scale=-2:{height}"] if height else []), str(p)],
            capture_output=True,
        )  # fmt: skip
        if r.returncode == 0 and p.exists():
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


STEADY_JITTER = 0.35  # a webcam face keeps its place and size within this share of its width


def steady_faces(boxes: list[Box], n_frames: int, fw: int, fh: int) -> list[Box]:
    """Webcam-sized faces that stay put across the sampled frames, most-seen first.

    A webcam overlay never moves; faces of game characters (GTA cutscenes) jump and rescale with every
    camera cut, so a cluster whose members drift or change size is not a cam (kaicenat, 19 Nov).
    """
    need = max(2, math.ceil(0.3 * n_frames))
    out: list[Box] = []
    for c in cluster_boxes(boxes):
        if len(c) < need:
            continue
        f: Box = tuple(statistics.median(b[i] for b in c) for i in range(4))  # type: ignore[assignment]
        if f[3] < 0.04 * fh or f[3] > FULL_CAM_FACE_H * fh:
            continue
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
        if all(
            math.hypot(b[0] + b[2] / 2 - cx, b[1] + b[3] / 2 - cy) <= STEADY_JITTER * f[2]
            and abs(b[3] - f[3]) <= STEADY_JITTER * f[3]
            for b in c
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
