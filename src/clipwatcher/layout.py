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

Which frames: with peak_s (media time of the moment) the ones from SCENE_BEFORE_S before to SCENE_AFTER_S
after it, without it frames spread over the whole media (calibration of a recording).
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
        # no overlay cam: a full-screen camera or IRL shot may be reframed to 9:16 around the person
        reframe = not self.has_webcam and self.source in ("fullcam", "no-face")
        return {
            "has_webcam": self.has_webcam, "cam": self.cam, "game": self.game, "face": self.face,
            "reframe": reframe,
        }  # fmt: skip


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
# With peak_s the scene is read from the frames just around the moment. The media can be far longer than
# the clip (the VOD lane's --video is the whole VOD, where frames spread over all of it showed scenes from
# hours away), and the scene at the moment is the one the clip is about.
SCENE_BEFORE_S, SCENE_AFTER_S = 8.0, 2.0


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


# The webcam's rectangle is read from the picture (calibrate.cam_rect): its border stays on the same pixels
# while the game around it changes, so the frames for it come from a longer stretch than the faces do.
# 20 Nov, live samples of the owner's streamers: every confirmed facecam_rect was somewhere else than the
# cam that day (zackrawrr, clix, tfue, ishowspeed) and kaicenat, "has_webcam: false", had one. So a
# confirmed rect counts only while the picture agrees with it, and has_webcam: false means "do not guess
# a cam from a face alone" (GTA characters), not "do not look".
EDGES_BEFORE_S, EDGES_AFTER_S = 30.0, 10.0
MORE_FACE_FRAMES = 12  # no cam found: look for the face in this many more frames of the scene
CUTOUT_SHARE = 0.6  # a face with no rectangle around it is a (cut-out) webcam only when seen this often
ZOOM_FRAMES = 6  # frames a rectangle is enlarged in to look for the face (half of them must show it)
SEEN_SURE = 0.8  # a face in this share of the frames needs no closer look
SMALL_CAM = 0.15  # a 'cam' this much smaller than the largest is a portrait in the HUD, not a webcam
CONTENT_AREA = 0.3  # share of the frame: larger rectangles with faces are content when a webcam is there too


@dataclass
class Cam:
    face: tuple[float, float, float, float]  # median face box
    rect: tuple[int, int, int, int]  # the webcam picture
    sure: float
    seen: float  # share of the frames looked at that show the face
    span: tuple[float, float, float, float]  # where the face went over the frames (x, y, w, h)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    union = a[2] * a[3] + b[2] * b[3] - ix * iy
    return ix * iy / union if union > 0 else 0.0


def _spread(paths: list, n: int) -> list:
    return [paths[int(i * len(paths) / n)] for i in range(min(n, len(paths)))]


def _cam_of(maps, boxes: list, share: float) -> Cam | None:
    """The webcam around a group of sightings of one face, or None: no rectangle there, the camera is the
    picture, the 'face' never moves (a portrait in a HUD), or the rectangle is part of a room."""
    import statistics

    f = tuple(statistics.median(b[i] for b in boxes) for i in range(4))
    if f[3] < 0.025 * maps.size[1] or not calibrate.face_alive(maps, f):  # type: ignore[arg-type]
        return None
    found = calibrate.cam_rect(maps, f)  # type: ignore[arg-type]
    if (
        not found
        or not calibrate.is_overlay(found[0], *maps.size)
        or calibrate.room_rectangle(maps, found[0])
    ):
        return None
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
    return Cam(f, found[0], found[1], share, (x0, y0, x1 - x0, y1 - y0))  # type: ignore[arg-type]


def find_cams(boxes: list, n_frames: int, maps, paths: list, detector, more: bool = True) -> list[Cam]:
    """The webcam overlays of a scene, most seen first.

    boxes: faces of a full-frame pass over n_frames frames; paths: the scene's frames. A face seen in
    nearly every frame with a webcam's rectangle around it is one. Any other candidate is looked at
    closely: the rectangle is enlarged in ZOOM_FRAMES frames and searched again. On 20 Nov the enlarged
    look found the face in 20 of 20 frames of every real cam tried (kaicenat's small dark one, tfue in
    profile: 0 or 1 of 20 in the full frame) and in 1 of 20 of a minimap the full-frame pass 'saw' faces
    in. Candidates are faces seen less often, then (more) rectangles that look like a webcam with no face
    seen at all, then faces seen twice in further frames."""
    if maps is None:
        return []
    look = _spread(paths, ZOOM_FRAMES)
    cams: list[Cam] = []

    def confirmed(rect) -> Cam | None:
        hits = calibrate.zoom_faces(look, detector, rect)
        if 2 * len(hits) < len(look):
            return None
        return _cam_of(maps, hits, len(hits) / len(look))

    def add(cam: Cam | None) -> None:
        if cam and all(_iou(cam.rect, o.rect) <= 0.5 for o in cams):
            cams.append(cam)

    need = max(1, -(-3 * n_frames // 10))  # ceil(0.3 n)
    for c in calibrate.cluster_boxes(boxes):
        if len(c) < need:
            continue
        cam = _cam_of(maps, c, len(c) / n_frames)
        if cam and cam.seen < SEEN_SURE:
            cam = confirmed(cam.rect)
        add(cam)
    largest = max((c.rect[2] * c.rect[3] for c in cams), default=0)
    for prop in calibrate.cam_proposals(maps) if more else ():
        if prop.rect[2] * prop.rect[3] < 0.6 * largest or any(_iou(prop.rect, o.rect) > 0.3 for o in cams):
            continue  # only where no cam is yet: the streamer's own (larger), or one beside it
        add(confirmed(prop.rect))
    if not cams and more:
        rest = [p for p in paths if p not in set(look)]
        both = boxes + [b for p in _spread(rest, MORE_FACE_FRAMES) for b in detector(p)]
        for c in calibrate.cluster_boxes(both):
            if len(c) >= 2:
                cam = _cam_of(maps, c, 0.0)
                add(confirmed(cam.rect) if cam else None)
    if any(c.sure > 0.5 for c in cams):  # a rectangle with a side that is only a line: a video's frame
        cams = [c for c in cams if c.sure > 0.5]
    fw, fh = maps.size
    own = [c for c in cams if c.rect[2] * c.rect[3] <= CONTENT_AREA * fw * fh]
    if own:  # beside a webcam, a far larger rectangle with a face in it is the video being watched
        cams = own
    largest = max((c.rect[2] * c.rect[3] for c in cams), default=0)
    cams = [c for c in cams if c.rect[2] * c.rect[3] >= SMALL_CAM * largest]  # portraits in a HUD
    return sorted(cams, key=lambda c: -c.seen)


def _overlay_layout(cams: list[Cam], pick: Cam, sc: list[dict], fw: int, fh: int, active=None) -> Layout:
    """Layout for a webcam whose rectangle was read from the picture. A confirmed scene with (nearly) the
    same rectangle keeps its name and its gameplay_rect; anything else is new and flagged for review."""
    game = calibrate.gameplay_crop([c.rect for c in cams], fw, fh, active=active)
    for s in sc:
        if not (s.get("has_webcam", True) and s.get("facecam_rect")):
            continue
        if _iou(_parse(s["facecam_rect"]), pick.rect) >= 0.85:
            return Layout(
                True,
                calibrate.rect_str(pick.rect),
                s.get("gameplay_rect") or calibrate.rect_str(game),
                f"scene:{s.get('name', '?')}",
                _span_str(pick),
            )
    return Layout(
        True,
        calibrate.rect_str(pick.rect),
        calibrate.rect_str(game),
        "multi-cam" if len(cams) >= 2 else "auto-new",
        _span_str(pick),
    )


def _span_str(cam: Cam) -> str:
    """Where the face went, clipped to the cam: the panel's crop is centred on it."""
    x, y, w, h = cam.span
    rx, ry, rw, rh = cam.rect
    x0, y0 = max(x, rx), max(y, ry)
    x1, y1 = min(x + w, rx + rw), min(y + h, ry + rh)
    if x1 - x0 < 2 or y1 - y0 < 2:
        x0, y0, x1, y1 = rx, ry, rx + rw, ry + rh
    return calibrate.rect_str((int(x0), int(y0), int(x1 - x0), int(y1 - y0)))


def resolve(
    media: Path, streamer: str, *, n_frames: int = 4, detector=None, peak_s: float | None = None,
    cam_hint: tuple[float, float] | None = None,
) -> Layout:  # fmt: skip
    scfg = config.streamer(streamer)
    sc = scenes(scfg)
    no_cam_cfg = scfg.get("has_webcam") is False and not scfg.get("layouts")
    if scfg.get("layout") == "fixed" and (sc or no_cam_cfg):
        s = sc[0] if sc else {}
        cam = None if no_cam_cfg else s.get("facecam_rect")
        return Layout(bool(cam), cam, s.get("gameplay_rect") or scfg.get("gameplay_rect"), "fixed")
    detector = detector or calibrate.make_detector()
    if detector is None:
        log.warning("no face detector (mediapipe + models/, or opencv): layout = the configured default")
        s = sc[0] if sc else {}
        return Layout(
            bool(s.get("has_webcam", False)), s.get("facecam_rect"), s.get("gameplay_rect"), "default"
        )
    window = (peak_s - SCENE_BEFORE_S, peak_s + SCENE_AFTER_S) if peak_s is not None else None
    wide = None
    if peak_s is not None:  # a moment near the start of the recording: a stretch as long, starting there
        lo = max(0.0, peak_s - EDGES_BEFORE_S)
        wide = (lo, lo + EDGES_BEFORE_S + EDGES_AFTER_S)
    with tempfile.TemporaryDirectory() as td:
        keys = calibrate.scene_frames(Path(media), Path(td) / "scene", wide)
        keys = calibrate.same_scene(keys, peak_s)  # a layout switched in or out of is another scene
        frames = [(t, p) for t, p in keys if window is None or window[0] <= t <= window[1]]
        if len(frames) > n_frames and window is None:
            frames = [frames[int((i + 0.5) * len(frames) / n_frames)] for i in range(n_frames)]
        if len(frames) < n_frames:  # few keyframes around the moment: exact frames, as before
            frames = calibrate.sample_frames(Path(media), Path(td), n_frames, window=window)
        if not frames:  # unreadable / too short media: fall back to the confirmed default scene
            s = sc[0] if sc else {}
            return Layout(
                bool(s.get("has_webcam", False)), s.get("facecam_rect"), s.get("gameplay_rect"), "default"
            )
        boxes = [b for _, p in frames for b in detector(p)]
        from PIL import Image

        with Image.open(frames[0][1]) as im:
            fw, fh = im.size
        maps = calibrate.scene_maps([p for _, p in keys]) if len(keys) >= 6 else None
        if maps is not None and maps.size != (fw, fh):
            maps = None
        face = calibrate.dominant_face(boxes, len(frames))
        in_scene = face is not None and match_scene(face, False, sc) is not None
        paths = [p for _, p in keys]
        found = find_cams(boxes, len(frames), maps, paths, detector, more=not in_scene)
        if not found and cam_hint and maps is not None and face is None:
            # the judge sees a webcam the detector missed: look closer there, for a face in a rectangle
            near = calibrate.face_near([p for _, p in frames], detector, cam_hint, fw, fh)
            cam = _cam_of(maps, [near], 1.0) if near is not None else None
            found = [cam] if cam else []
        if found:
            pick = _pick_cam(found, media, fw, fh, peak_s, cam_hint, Path(td))
            return _overlay_layout(found, pick, sc, fw, fh, maps.active if maps else None)
        if no_cam_cfg:
            # Confirmed no-webcam streamer and no overlay in the picture: a face alone is not a cam here
            # (GTA characters' faces look like one).
            return Layout(False, None, scfg.get("gameplay_rect"), "fixed")
        if maps is None:  # the picture could not be read for rectangles: faces alone decide, as before
            return _face_only_layout(media, boxes, frames, detector, sc, fw, fh, peak_s, cam_hint, Path(td))
        # No rectangle around any face: a cut-out cam, a full-screen camera, or no cam at all.
        if in_scene:  # a confirmed scene whose rectangle the picture does not show (soft or no border)
            hit = match_scene(face, False, sc)
            return Layout(
                True, hit.get("facecam_rect"), hit.get("gameplay_rect"), f"scene:{hit.get('name', '?')}",
                calibrate.rect_str(tuple(int(v) for v in face)),  # type: ignore[arg-type]
            )  # fmt: skip
        stable = calibrate.stable_faces(boxes, len(frames), fw, fh)
        cuts = [f for f in stable if _cutout_like(f, boxes, len(frames), maps)]
        if cuts:
            pick = cuts[0]
            if len(cuts) >= 2 and (peak_s is not None or cam_hint is not None):
                pick = reacting_face(media, cuts, fw, fh, peak_s, cam_hint, Path(td))
            boxes_of = [calibrate.cutout_box(f, fw, fh) for f in cuts]
            return Layout(
                True,
                calibrate.rect_str(calibrate.cutout_box(pick, fw, fh)),
                calibrate.rect_str(calibrate.gameplay_crop(boxes_of, fw, fh)),
                "multi-cam" if len(cuts) >= 2 else "auto-new",
                calibrate.rect_str(tuple(int(v) for v in pick)),  # type: ignore[arg-type]
            )
        if face is None and cam_hint:  # the judge sees a webcam nothing above found
            near = calibrate.face_near([p for _, p in frames], detector, cam_hint, fw, fh)
            fs = 0.09 * fh  # nothing there either: a typical webcam face at the judge's point
            at = near or (cam_hint[0] * fw - fs / 2, cam_hint[1] * fh - fs / 2, fs, fs)
            cam = calibrate.cutout_box(at, fw, fh)
            return Layout(
                True,
                calibrate.rect_str(cam),
                calibrate.rect_str(calibrate.gameplay_crop([cam], fw, fh)),
                "auto-new" if near else "judge-cam",
                calibrate.rect_str(tuple(int(v) for v in near)) if near else None,  # type: ignore[arg-type]
            )
        if face is None:
            # A missed face (looking down, in profile, a headset) is far likelier than the cam vanishing.
            # The streamer's confirmed cam scene stands while the picture has an overlay with those sides;
            # a streamer known to have a cam gets the rectangle that looks like one.
            cam_scenes = [s for s in sc if s.get("has_webcam", True) and s.get("facecam_rect")]
            for s in cam_scenes:
                if calibrate.rect_present(maps, _parse(s["facecam_rect"])):
                    return Layout(True, s.get("facecam_rect"), s.get("gameplay_rect"), "default-noface")
            likely = [r for r in calibrate.cam_proposals(maps) if 1.2 <= r.rect[2] / r.rect[3] <= 1.9]
            if cam_scenes and likely:
                game = calibrate.gameplay_crop([likely[0].rect], fw, fh)
                return Layout(
                    True, calibrate.rect_str(likely[0].rect), calibrate.rect_str(game), "default-noface"
                )
            no_cam = next((s for s in sc if s.get("has_webcam") is False), None)
            return Layout(False, None, no_cam.get("gameplay_rect") if no_cam else None, "no-face")
        # A face with nothing around it that says overlay: the camera is the picture (a big face, or the
        # frame's own sides around it, or someone sitting in a room that stays still), else a face that
        # comes and goes (the game's, a passer-by's): no webcam.
        around = calibrate.cam_rect(maps, face)
        seen = sum(1 for b in boxes if abs(b[0] - face[0]) <= face[2] and abs(b[1] - face[1]) <= face[3])
        full_cam = (
            face[3] > calibrate.FULL_CAM_FACE_H * fh
            or (around is not None and not calibrate.is_overlay(around[0], fw, fh))
            or (seen >= CUTOUT_SHARE * len(frames) and calibrate.scene_still(maps))
        )
        no_cam = match_scene(face, True, sc) if full_cam else None
        if no_cam:
            return Layout(False, None, no_cam.get("gameplay_rect"), f"scene:{no_cam.get('name', '?')}")
        return Layout(False, None, None, "fullcam" if full_cam else "no-face")


def _pick_cam(found: list[Cam], media: Path, fw: int, fh: int, peak_s, cam_hint, td: Path) -> Cam:
    """Of several webcams the one for the layout: the judge's (cam_hint); else the one whose face is
    there far more often than the others' (a video the streamer watches shows faces too, on and off);
    else the one clearly larger than the rest (a small one beside the streamer's own is a guest's, or a
    cam inside what is being watched); else, between cams of a size, whoever moves most at the moment."""
    if len(found) == 1:
        return found[0]
    if cam_hint is None:
        by_seen = sorted(found, key=lambda c: -c.seen)
        if by_seen[0].seen >= 1.5 * by_seen[1].seen:
            return by_seen[0]
        by_size = sorted(found, key=lambda c: -c.rect[2] * c.rect[3])
        if by_size[0].rect[2] * by_size[0].rect[3] >= 1.5 * by_size[1].rect[2] * by_size[1].rect[3]:
            return by_size[0]
        if peak_s is None:
            return found[0]
    f = reacting_face(media, [c.face for c in found], fw, fh, peak_s, cam_hint, td)
    return next(c for c in found if c.face == f)


def _cutout_like(face, boxes: list, n_frames: int, maps) -> bool:
    """A face without a rectangle around it is a cut-out webcam (green screen over the game) when it
    stays put in a lower corner of the picture, frame after frame, and what is right around the head
    moves like a game. Around a head in a room the wall stands still, however much the people move
    (yurnboi, easyallies: people at the sides of a full-screen camera were taken for cut-outs)."""
    import numpy as np

    fw, fh = maps.size
    x, y, w, h = (int(v) for v in face)
    cx = x + w / 2
    if 0.28 * fw < cx < 0.72 * fw or y + h / 2 < 0.5 * fh:
        return False  # in the middle, or up where heads are in a room: a cut-out sits in a lower corner
    seen = sum(1 for b in boxes if abs(b[0] - x) <= w and abs(b[1] - y) <= h)
    if seen < CUTOUT_SHARE * n_frames or not calibrate.face_alive(maps, face):
        return False
    game = float(np.median(maps.sd))
    if game < calibrate.GAME_SD:
        return False
    sd = maps.sd
    beside = [  # left of, right of and above the head, one head away
        sd[max(0, y - h // 2) : y + h, max(0, x - 2 * w) : max(0, x - w)],
        sd[max(0, y - h // 2) : y + h, x + 2 * w : x + 3 * w],
        sd[max(0, y - 2 * h) : max(0, y - h), max(0, x - w) : x + 2 * w],
    ]
    beside = [b.ravel() for b in beside if b.size]
    return bool(beside) and float(np.median(np.concatenate(beside))) >= 0.5 * game


def _face_only_layout(media, boxes, frames, detector, sc, fw, fh, peak_s, cam_hint, td: Path) -> Layout:
    """The layout from faces alone (no numpy, or too few frames to read rectangles from): a small steady
    face is a webcam and its crop is guessed from the face, a big one a full-screen camera."""
    face = calibrate.dominant_face(boxes, len(frames))
    cams = [
        f
        for f in calibrate.stable_faces(boxes, len(frames), fw, fh)
        if f[3] <= calibrate.FULL_CAM_FACE_H * fh
    ]
    multi = len(cams) >= 2 and (peak_s is not None or cam_hint is not None)
    if multi:
        face = reacting_face(media, cams, fw, fh, peak_s, cam_hint, td)
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
    cam = calibrate.facecam_from_face(face, fw, fh)
    if judge_cam:
        game = calibrate.gameplay_avoiding(cam, fw, fh)
        return Layout(True, calibrate.rect_str(cam), calibrate.rect_str(game), "judge-cam", None)
    if multi:  # the reacting cam is not a confirmed scene: crop it from its face
        others = [calibrate.facecam_from_face(f, fw, fh) for f in cams]
        game = calibrate.gameplay_between(others, fw, fh) or calibrate.gameplay_avoiding(cam, fw, fh)
        return Layout(True, calibrate.rect_str(cam), calibrate.rect_str(game), "multi-cam", fstr)
    # New, unconfirmed scene: still rendered, but flagged in meta so an operator checks the crop.
    game = calibrate.gameplay_avoiding(cam, fw, fh)
    return Layout(True, calibrate.rect_str(cam), calibrate.rect_str(game), "auto-new", fstr)
