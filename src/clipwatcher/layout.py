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

Several webcams on screen (duo streams, co-streamers, guests): the cam that goes in the layout is the one of
the person who makes the moment. With peak_s given, frames around the peak decide: the face that moves most
there (laughing, shouting, jumping) wins (owner, 17 Nov).
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
    source: str  # fixed | scene:<name> | auto-new | multi-cam | no-face | default-noface | fullcam | default
    face: str | None = None  # "w:h:x:y" of the detected face (cold open centres on it)

    def as_render_kwargs(self) -> dict:
        return {"has_webcam": self.has_webcam, "cam": self.cam, "game": self.game, "face": self.face}


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


PEAK_OFFSETS = (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0)


STATIC_MOTION = 3.0  # in-box change below this = a still object taken for a face (posters, vests on a wall)
HINT_RADIUS = 0.25  # the judge's cam point must be this close (share of frame width) to a face


def reacting_face(media: Path, faces: list, fw: int, fh: int, peak_s: float | None, hint, td: Path):
    """Of several webcam faces, the one of whoever makes the moment.

    hint: the judge's (x, y) 0-1 centre of that webcam (it sees the frames and knows who reacts); the nearest
    face wins. Otherwise the face changing most inside its own box around peak_s (laughing, shouting),
    after dropping still objects the detector took for faces.
    """
    if hint:
        hx, hy = hint[0] * fw, hint[1] * fh
        d = [((f[0] + f[2] / 2 - hx) ** 2 + (f[1] + f[3] / 2 - hy) ** 2) ** 0.5 for f in faces]
        i = min(range(len(faces)), key=d.__getitem__)
        if d[i] <= HINT_RADIUS * fw:
            log.info("multi-cam: %d faces, judge picked %s", len(faces), faces[i][:2])
            return faces[i]
    if peak_s is None:
        return faces[0]
    peak = calibrate.frames_at(Path(media), td / "peak", [peak_s + o for o in PEAK_OFFSETS], height=None)
    if len(peak) < 3:
        return faces[0]
    scores = [calibrate.face_motion([p for _, p in peak], f, fw, pad=0.1) for f in faces]
    live = [i for i, v in enumerate(scores) if v >= STATIC_MOTION] or list(range(len(faces)))
    log.info("multi-cam: %d faces, motion %s", len(faces), [round(v, 1) for v in scores])
    return faces[max(live, key=scores.__getitem__)]


def resolve(
    media: Path, streamer: str, *, n_frames: int = 4, detector=None, peak_s: float | None = None,
    cam_hint: tuple[float, float] | None = None,
) -> Layout:  # fmt: skip
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
        if not frames:  # unreadable / too short media: fall back to the confirmed default scene
            s = sc[0] if sc else {}
            return Layout(
                bool(s.get("has_webcam", False)), s.get("facecam_rect"), s.get("gameplay_rect"), "default"
            )
        boxes = [b for _, p in frames for b in detector(p)]
        from PIL import Image

        with Image.open(frames[0][1]) as im:
            fw, fh = im.size
        face = calibrate.dominant_face(boxes, len(frames))
        cams = [
            f
            for f in calibrate.stable_faces(boxes, len(frames), fw, fh)
            if f[3] <= calibrate.FULL_CAM_FACE_H * fh
        ]
        multi = len(cams) >= 2 and (peak_s is not None or cam_hint is not None)
        if multi:
            face = reacting_face(media, cams, fw, fh, peak_s, cam_hint, Path(td))
        judge_cam = False
        if face is None and cam_hint:  # the judge sees a webcam the detector missed: look closer there
            face = calibrate.face_near([p for _, p in frames], detector, cam_hint, fw, fh)
            if face is None:  # still nothing: a typical webcam face at the judge's point
                fs = 0.09 * fh
                face, judge_cam = (cam_hint[0] * fw - fs / 2, cam_hint[1] * fh - fs / 2, fs, fs), True
    full_cam = bool(face) and face[3] > calibrate.FULL_CAM_FACE_H * fh
    fstr = calibrate.rect_str(tuple(int(v) for v in face)) if face and not full_cam else None  # type: ignore[arg-type]
    hit = match_scene(face, full_cam, sc)
    if hit:
        return Layout(
            bool(hit.get("has_webcam", True)),
            hit.get("facecam_rect"),
            hit.get("gameplay_rect"),
            f"scene:{hit.get('name', '?')}",
            fstr,
        )
    if face is None and not full_cam:
        # A missed face (looking down, small cam, headset) is far likelier than the cam vanishing: keep the
        # streamer's confirmed cam scene instead of dropping the cam, and flag it for the operator.
        cam_scene = next((s for s in sc if s.get("has_webcam", True) and s.get("facecam_rect")), None)
        if cam_scene:
            return Layout(
                True, cam_scene.get("facecam_rect"), cam_scene.get("gameplay_rect"), "default-noface"
            )
    if face is None or full_cam:
        return Layout(False, None, None, "fullcam" if full_cam else "no-face")
    if judge_cam and not hit:
        cam = calibrate.facecam_from_face(face, fw, fh)
        return Layout(
            True,
            calibrate.rect_str(cam),
            calibrate.rect_str(calibrate.gameplay_avoiding(cam, fw, fh)),
            "judge-cam",
            None,
        )
    if multi:  # the reacting cam is not a confirmed scene: crop it from its face
        cam = calibrate.facecam_from_face(face, fw, fh)
        others = [calibrate.facecam_from_face(f, fw, fh) for f in cams]
        game = calibrate.gameplay_between(others, fw, fh) or calibrate.gameplay_avoiding(cam, fw, fh)
        return Layout(
            True,
            calibrate.rect_str(cam),
            calibrate.rect_str(game),
            "multi-cam",
            fstr,
        )
    # New, unconfirmed scene: still rendered, but flagged in meta so an operator checks the crop.
    cam = calibrate.facecam_from_face(face, fw, fh)
    return Layout(
        True,
        calibrate.rect_str(cam),
        calibrate.rect_str(calibrate.gameplay_avoiding(cam, fw, fh)),
        "auto-new",
        fstr,
    )
