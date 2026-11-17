"""Per-clip layout resolution.

Every streamer arranges webcam, chat and alerts differently, and one streamer switches scenes mid-stream
(gameplay with a cam overlay, full-screen camera, no camera at all). So the layout is resolved for each
clip from its own frames and matched against the streamer's confirmed scenes in streamers.yaml:

  streamers:
    name:
      layout: auto            # auto (default) | fixed
      layouts:                # confirmed scenes, matched by where the face is
        - {name: gameplay, facecam_rect: "w:h:x:y", gameplay_rect: "w:h:x:y"}
        - {name: fullcam, has_webcam: false}

A single facecam_rect / gameplay_rect at the streamer level counts as one confirmed scene.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from clipwatcher import calibrate, config

log = logging.getLogger(__name__)


@dataclass
class Layout:
    has_webcam: bool
    cam: str | None
    game: str | None
    source: str  # fixed | scene:<name> | auto-new | no-face | fullcam | default

    def as_render_kwargs(self) -> dict:
        return {"has_webcam": self.has_webcam, "cam": self.cam, "game": self.game}


def _parse(r: str) -> tuple[int, int, int, int]:
    w, h, x, y = (int(float(v)) for v in r.split(":"))
    return x, y, w, h


def scenes(scfg: dict) -> list[dict]:
    out = list(scfg.get("layouts") or [])
    if scfg.get("facecam_rect") or scfg.get("has_webcam") is False:
        out.append(
            {
                "name": "default",
                "facecam_rect": scfg.get("facecam_rect"),
                "gameplay_rect": scfg.get("gameplay_rect"),
                "has_webcam": scfg.get("has_webcam", True) and bool(scfg.get("facecam_rect")),
            }
        )
    return out


def match_scene(
    face: tuple[float, float, float, float] | None, full_cam: bool, sc: list[dict]
) -> dict | None:
    """Confirmed scene whose facecam holds the face centre; no face / full camera -> a no-cam scene."""
    if face is None or full_cam:
        return next((s for s in sc if s.get("has_webcam") is False), None)
    cx, cy = face[0] + face[2] / 2, face[1] + face[3] / 2
    for s in sc:
        if s.get("has_webcam", True) and s.get("facecam_rect"):
            x, y, w, h = _parse(s["facecam_rect"])
            if x <= cx <= x + w and y <= cy <= y + h:
                return s
    return None


def resolve(media: Path, streamer: str, *, n_frames: int = 4, detector=None) -> Layout:
    scfg = config.streamer(streamer)
    sc = scenes(scfg)
    if scfg.get("has_webcam") is False and not scfg.get("layouts"):
        # Confirmed no-webcam streamer: never trust face detection (GTA characters' faces look like a cam).
        return Layout(False, None, scfg.get("gameplay_rect"), "fixed")
    if scfg.get("layout") == "fixed" and sc:
        s = sc[0]
        return Layout(bool(s.get("has_webcam", True)), s.get("facecam_rect"), s.get("gameplay_rect"), "fixed")
    detector = detector or calibrate.make_detector()
    if detector is None:
        s = sc[0] if sc else {}
        return Layout(
            bool(s.get("has_webcam", False)), s.get("facecam_rect"), s.get("gameplay_rect"), "default"
        )
    with tempfile.TemporaryDirectory() as td:
        frames = calibrate.sample_frames(Path(media), Path(td), n_frames)
        boxes = [b for _, p in frames for b in detector(p)]
        from PIL import Image

        with Image.open(frames[0][1]) as im:
            fw, fh = im.size
    face = calibrate.dominant_face(boxes, len(frames))
    full_cam = bool(face) and face[3] > calibrate.FULL_CAM_FACE_H * fh
    hit = match_scene(face, full_cam, sc)
    if hit:
        return Layout(
            bool(hit.get("has_webcam", True)),
            hit.get("facecam_rect"),
            hit.get("gameplay_rect"),
            f"scene:{hit.get('name', '?')}",
        )
    if face is None or full_cam:
        return Layout(False, None, None, "fullcam" if full_cam else "no-face")
    rects = calibrate.compute_rects(boxes, fw, fh, len(frames))
    # New, unconfirmed scene: still rendered, but flagged in meta so an operator checks the crop.
    return Layout(True, calibrate.rect_str(rects.facecam), calibrate.rect_str(rects.gameplay), "auto-new")
