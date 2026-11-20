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
    # fixed | scene:<name> | auto-new | multi-cam | default-noface | default: a webcam panel (or the
    # operator's no-cam scene); fullcam: a camera that is the picture, followed full screen; no-face: no
    # webcam seen; unsure: there may be a webcam but nothing sure enough to show as one. no-face and
    # unsure render the whole 16:9 frame over its blurred copy, which is never wrong (meta.layout).
    source: str
    face: str | None = None  # "w:h:x:y" of the detected face (cold open centres on it)
    sure: float = 1.0  # how sure the decision is, 0-1 (under 0.5: unsure)
    doubts: tuple[str, ...] = ()  # why it is unsure, in words (logged)
    evidence: dict | None = None  # the numbers behind it (logs, scripts/webcam_check.py)

    def as_render_kwargs(self) -> dict:
        # no overlay cam: a full-screen camera is reframed to 9:16 around the person. Not "no-face" any
        # more (20 Nov): with no steady face the layout cannot tell a handheld camera from a game, and the
        # renderer then followed faces of the game, or a webcam the layout had missed.
        reframe = not self.has_webcam and self.source == "fullcam"
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
SIDE_FAINT = 0.55  # a side that is an edge in less than this share is faint ...
ALIVE = 0.6  # ... which counts against the cam when its inside changes this much of what is around it
STRIP_LIVE = 3.0  # a strip behind a line across the cam that moves this much more than the rest ...
STRIP_LINE, STRIP_DEPTH = 0.45, (0.04, 0.4)  # ... a line this steady, this share of the rectangle from a side
SIZE_ALIKE = 1.5  # cams within this factor in area are cams of one layout (a duo, a grid)
CAM_MIN_AREA = 0.01  # a 'cam' under this share of the frame is a portrait in the HUD
CUTOUT_MAX_FACE = 0.26  # a cut-out's face is at most this share of the frame's height
GAME_STEADY = 0.02  # this share of the picture on steady edges: a HUD, so a game
SPLIT_MAX = 0.9  # a rectangle around the face is a part of the picture up to this share of it (black
# bars that stay are not picture); above, it is the picture itself
GONE_RUN = 3  # the cam's border missing in this many frames in a row: the layout changes in the stretch
CONTENT_AREA = 0.3  # share of the frame: larger rectangles with faces are content when a webcam is there too


@dataclass
class Cam:
    face: tuple[float, float, float, float]  # median face box
    rect: tuple[int, int, int, int]  # the webcam picture
    sure: float
    seen: float  # share of the frames looked at that show the face
    span: tuple[float, float, float, float]  # where the face went over the frames (x, y, w, h)
    spread: float = 0.0  # how far it went, in face sizes (icons on a minimap are 'faces' all over it)
    full: float = 0.0  # share of the frames in which the full-frame pass saw the face (0: only enlarged)


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
    spread = max((x1 - x0 - f[2]) / f[2], (y1 - y0 - f[3]) / f[3])
    return Cam(f, found[0], found[1], share, (x0, y0, x1 - x0, y1 - y0), spread)  # type: ignore[arg-type]


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
        if cam:
            cam.full = len(c) / n_frames
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
    cams = [  # portraits in a HUD
        c for c in cams if c.rect[2] * c.rect[3] >= max(SMALL_CAM * largest, CAM_MIN_AREA * fw * fh)
    ]
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


def _ltrb(r) -> tuple[int, int, int, int]:
    return r[0], r[1], r[0] + r[2], r[1] + r[3]


def _cam_evidence(maps, pick: Cam, found: list[Cam]) -> dict:
    """calibrate.cam_evidence for the cam that was picked, plus how it and the others were found."""
    e = calibrate.cam_evidence(maps, pick.rect, pick.face)
    others = []
    for c in found:
        o = calibrate.cam_evidence(maps, c.rect, c.face)
        others.append({
            "rect": list(c.rect), "seen": round(c.seen, 2), "sure": round(c.sure, 3),
            "spread": round(c.spread, 2), "live": o["live"], "inside_sd": o["inside_sd"],
            "face_h": o["face_h"], "outer": o["outer"][:2],
        })  # fmt: skip
    e.update(
        seen=round(pick.seen, 2), sure=round(pick.sure, 3), spread=round(pick.spread, 2), cams=others,
        full=round(pick.full, 2),
        face=[round(v) for v in pick.face],
    )  # fmt: skip
    return e


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
    """The layout of one clip. Layout.sure says how sure it is; what the picture cannot decide comes out
    as source "unsure" (no cam panel, no reframe: the whole frame), with the reasons in Layout.doubts and
    in the log, so a Short never shows something else as the webcam. See doubts() and _decide()."""
    ev: dict = {}
    lay = _resolve(
        media, streamer, ev, n_frames=n_frames, detector=detector, peak_s=peak_s, cam_hint=cam_hint
    )
    if lay.evidence is None:
        lay.evidence = ev or None
    else:
        lay.evidence.update(ev)
    if lay.source == "unsure":
        log.info("layout unsure for %s at %s: %s", streamer, peak_s, "; ".join(lay.doubts))
    return lay


def _resolve(
    media: Path, streamer: str, ev: dict, *, n_frames: int = 4, detector=None, peak_s: float | None = None,
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
        stretch = len(keys)
        times, whole = [t for t, _ in keys], [p for _, p in keys]
        keys = calibrate.same_scene(keys, peak_s)  # a layout switched in or out of is another scene
        ev.update(frames=len(keys), switched=len(keys) < stretch)
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
        paths = [p for _, p in keys]
        lay = _decide(media, boxes, frames, paths, maps, detector, sc, scfg, peak_s, cam_hint, Path(td), ev)
        # One layout is rendered for the whole clip, so it has to hold for the whole stretch.
        if lay.has_webcam and lay.evidence and "rect" in lay.evidence:
            at_moment = [p for _, p in frames if p in set(whole)] or paths
            gone = _gone(whole, tuple(lay.evidence["rect"]), at_moment)  # type: ignore[arg-type]
            if gone:
                return _unsure([gone], lay.sure, lay.evidence)
        if lay.has_webcam and ev.get("cutout"):
            # A cut-out webcam is there through the whole stretch, at its place; a person in a broadcast
            # or in a video is there for a shot (leodsumo: a spectator at the ring).
            fx, fy, fwd, fht = ev["cutout"]
            look = _spread(whole, ZOOM_FRAMES)
            hits = calibrate.zoom_faces(look, detector, _parse(lay.cam))
            there = [b for b in hits if abs(b[0] - fx) <= fwd and abs(b[1] - fy) <= fht]
            ev.update(cutout_there=round(len(there) / max(1, len(look)), 2))
            if len(there) < SEEN_SURE * len(look):
                n = f"{len(there)} of {len(look)}"
                return _unsure(
                    [f"a face placed like a cut-out webcam, there in {n} frames of the stretch"], 0.3, None
                )
        rest = [(t, p) for t, p in zip(times, whole, strict=True) if p not in set(paths)]
        if lay.source == "fullcam" and len(rest) >= 6:  # a full-screen camera now; and before the cut?
            other_maps = calibrate.scene_maps([p for _, p in rest])
            look = [rest[int((i + 0.5) * len(rest) / n_frames)] for i in range(min(n_frames, len(rest)))]
            if other_maps is not None and other_maps.size == (fw, fh):
                other = _decide(
                    media, [b for _, p in look for b in detector(p)], look, [p for _, p in rest],
                    other_maps, detector, sc, scfg, None, None, Path(td), {},
                )  # fmt: skip
                if other.has_webcam or "rect" in (other.evidence or {}):
                    why = (
                        "another layout in the stretch has a webcam: the camera is not the picture throughout"
                    )
                    return _unsure([why], lay.sure, None)
        return lay


def doubts(e: dict, hinted: bool = False) -> list[str]:
    """What speaks against showing a found rectangle as the webcam (e = _cam_evidence); an empty list
    means sure. Each of these was true of panels that showed something else on the labelled streams (20
    Nov, 447 moments of 91 channels) far more often than of panels that were right:
    - a side that is only a faint line, on a rectangle with three sure sides or one whose inside moves
      like what is around it: the side is a line of the game or of the room (sodapoppin, xaryu: the
      rectangle ran on to the frame's edge; k4sen: a minimap). A side put where a banner begins is sure;
    - the face is there in too few of the frames looked at (a video that is being watched);
    - a rectangle with three sure sides in which only the enlarged look found a face (787prada: an empty
      inventory slot). Either alone is common among real cams; together nothing else holds the panel;
    - behind a line across the rectangle a strip moves far more than the rest: a second camera attached
      to the cam (infume's keyboard cam), or the hands on the desk: the picture cannot say which;
    - a line across the rectangle at which its own sides change, too deep to be cut off as a bar: a
      sponsor panel as tall as half the cam (genburten) or the edge of a wall (infume): the picture
      does not say which;
    - several rectangles with a face, of different sizes, and no judge to say which is the streamer's
      (hasanabi: a video window; warn, pikabooirl: the stream being watched)."""
    out = []
    lines = [v for k, v in e["sides"].items() if k not in e.get("banner", ())]
    side = min(lines, default=1.0)
    if side < SIDE_FAINT and (e["sure"] <= 0.5 or e["live"] >= ALIVE):
        out.append(f"a side is only a faint line ({side:.2f})")
    if e["seen"] < SEEN_SURE:
        out.append(f"the face is in {e['seen']:.0%} of the frames looked at")
    if e["sure"] <= 0.5 and e.get("full", 1.0) == 0:
        out.append("three sure sides, and a face in it only when enlarged")
    for k, ratio in (e.get("strips") or {}).items():
        cov, depth, _jump = e["chords"][k]
        if ratio >= STRIP_LIVE and cov >= STRIP_LINE and STRIP_DEPTH[0] <= depth <= STRIP_DEPTH[1]:
            out.append("behind a line across the rectangle a strip moves far more than the rest of it")
            break
    for cov, depth, jump in e["chords"].values():
        if cov >= calibrate.CHORD_CUT and depth > calibrate.CHORD_DEEP and (jump or 0) >= calibrate.SIDE_JUMP:
            out.append("a line across the rectangle, more than a third in, at which its sides change")
            break
    cams = sorted(e["cams"], key=lambda c: -c["seen"])
    sizes = sorted(c["rect"][2] * c["rect"][3] for c in cams)
    clear = len(cams) >= 2 and cams[0]["seen"] >= 1.5 * cams[1]["seen"] and cams[0]["rect"] == e["rect"]
    if len(cams) >= 2 and sizes[-1] >= SIZE_ALIKE * sizes[0] and not hinted and not clear:
        out.append("several rectangles with a face in them, of different sizes: which is the streamer's?")
    return out


def _unsure(why: list[str], sure: float, evidence: dict | None) -> Layout:
    """The whole frame, no cam panel and no reframe: the layout that is never wrong."""
    return Layout(False, None, None, "unsure", None, round(min(sure, 0.45), 3), tuple(why), evidence)


def _gone(frames: list, rect: tuple[int, int, int, int], scene: list | None = None) -> str | None:
    """Why the webcam found is not sure for the whole stretch, or None: its border, as clear as in the
    frames the layout was read from (scene; default: all of them), is missing in GONE_RUN frames in a
    row. The streamer switches layout within the stretch, and one layout for the whole clip would show
    the wrong thing in part of it."""
    import statistics

    shares = calibrate.rect_in_frames(frames, rect)
    if len(shares) < 2 * GONE_RUN:
        return None
    own = [v for p, v in zip(frames, shares, strict=True) if p in set(scene)] if scene else shares
    usual = statistics.median(own or shares)
    run = longest = 0
    for v in shares:
        run = run + 1 if v < 0.4 * usual else 0
        longest = max(longest, run)
    if longest >= GONE_RUN:
        return f"the cam's border is missing in {longest} frames in a row of the stretch: the layout changes"
    return None


def _decide(
    media: Path, boxes: list, frames: list, paths: list, maps, detector, sc: list[dict], scfg: dict,
    peak_s: float | None, cam_hint, td: Path, ev: dict,
) -> Layout:  # fmt: skip
    """The layout from what was gathered: boxes = faces of the full-frame pass over frames [(t, path)]
    around the moment, paths = the frames of the scene, maps = what stays put in them (None when it
    could not be read). Split from resolve so a decision can be replayed on kept evidence."""
    if maps is not None:
        fw, fh = maps.size
    else:
        from PIL import Image

        with Image.open(frames[0][1]) as im:
            fw, fh = im.size
    no_cam_cfg = scfg.get("has_webcam") is False and not scfg.get("layouts")
    face = calibrate.dominant_face(boxes, len(frames))
    in_scene = face is not None and match_scene(face, False, sc) is not None
    found = find_cams(boxes, len(frames), maps, paths, detector, more=not in_scene)
    if not found and cam_hint and maps is not None and face is None:
        # the judge sees a webcam the detector missed: look closer there, for a face in a rectangle
        near = calibrate.face_near([p for _, p in frames], detector, cam_hint, fw, fh)
        cam = _cam_of(maps, [near], 1.0) if near is not None else None
        found = [cam] if cam else []
    if maps is not None:
        stable = calibrate.stable_faces(boxes, len(frames), fw, fh)
        ev.update(
            scene_sd=round(calibrate.scene_spread(maps), 2),
            proposals=[list(r.rect) for r in calibrate.cam_proposals(maps)],
            steady=round(calibrate.steady_share(maps), 4),
            faces=[[round(v) for v in f] for f in stable],
        )
    if found:
        pick = _pick_cam(found, media, fw, fh, peak_s, cam_hint, td)
        lay = _overlay_layout(found, pick, sc, fw, fh, maps.active if maps else None)
        lay.evidence = _cam_evidence(maps, pick, found)
        lay.sure = round(pick.sure, 3)
        why = doubts(lay.evidence, hinted=cam_hint is not None)
        if why and not lay.source.startswith("scene:"):  # a confirmed scene the picture agrees with stands
            return _unsure(why, pick.sure, lay.evidence)
        return lay
    if no_cam_cfg:
        # Confirmed no-webcam streamer and no overlay in the picture: a face alone is not a cam here
        # (GTA characters' faces look like one).
        return Layout(False, None, scfg.get("gameplay_rect"), "fixed")
    if maps is None:  # too few frames to read rectangles from: a face alone says where no webcam ends
        if face is None:
            return Layout(False, None, None, "no-face")
        return _unsure(["too few frames to read the webcam's rectangle from the picture"], 0.2, None)
    # No rectangle around any face: a cut-out cam, a full-screen camera, or no cam at all.
    if in_scene:
        # The face is where a confirmed scene has its cam, and no rectangle could be read around it. The
        # confirmed one counts while its sides are in the picture; else it is a soft-edged cam or a stale
        # entry (20 Nov: four of four were), and `layout: fixed` is the operator's word against the picture.
        hit = match_scene(face, False, sc)
        if calibrate.rect_present(maps, _parse(hit["facecam_rect"])):
            return Layout(
                True, hit.get("facecam_rect"), hit.get("gameplay_rect"), f"scene:{hit.get('name', '?')}",
                calibrate.rect_str(tuple(int(v) for v in face)),  # type: ignore[arg-type]
            )  # fmt: skip
        name = hit.get("name", "?")
        why = (
            f"a face where the confirmed scene '{name}' has its cam, but that rectangle is not in the picture"
        )
        return _unsure([why], 0.4, None)

    def seen(f) -> int:
        return sum(1 for b in boxes if abs(b[0] - f[0]) <= f[2] and abs(b[1] - f[1]) <= f[3])

    steady = [
        f for f in calibrate.stable_faces(boxes, len(frames), fw, fh) if seen(f) >= CUTOUT_SHARE * len(frames)
    ]
    # A rectangle around a face that find_cams did not take as a webcam (the face barely moves, the
    # rectangle looks like part of a room, it is half the frame) but that could be one: a split screen,
    # a cam over a still game. While there is one the camera is not surely the whole picture.
    held = _could_be_cam(maps, [f for f in [face, *steady] if f is not None])
    ev.update(could_be_cam=list(held) if held else None)
    full_scene = next(  # a confirmed scene with no webcam and no gameplay crop: the camera is the picture
        (s for s in scfg.get("layouts") or [] if s.get("has_webcam") is False and not s.get("gameplay_rect")),
        None,
    )
    open_question = (  # nothing around the face that is the size of a webcam and has drawn sides
        held is None or calibrate.room_rectangle(maps, held) or held[2] * held[3] > CONTENT_AREA * fw * fh
    )
    if full_scene and face is not None and open_question and not calibrate.cam_proposals(maps):
        # The operator confirmed that this streamer films full screen (D12: followed full screen). The
        # picture cannot tell one face in a moving picture from a webcam it failed to read, a doorway
        # from a cam with a faint side, or a camera beside a panel from a split screen; with no
        # webcam-sized rectangle of drawn sides and nothing that looks like a cam in it, the confirmed
        # scene decides.
        ev.update(confirmed=full_scene.get("name", "?"))
        return Layout(False, None, None, "fullcam", None, 0.9)
    if len(steady) == 1 and _cutout_like(steady[0], maps):
        cam = calibrate.cutout_box(steady[0], fw, fh)
        ev.update(cutout=[int(v) for v in steady[0]])
        return Layout(
            True,
            calibrate.rect_str(cam),
            calibrate.rect_str(calibrate.gameplay_crop([cam], fw, fh)),
            "auto-new",
            calibrate.rect_str(tuple(int(v) for v in steady[0])),  # type: ignore[arg-type]
            0.6,
        )
    if face is None:
        # A missed face (looking down, in profile, a headset) is far likelier than the cam vanishing: the
        # streamer's confirmed cam scene stands while the picture has an overlay with those sides.
        cam_scenes = [s for s in sc if s.get("has_webcam", True) and s.get("facecam_rect")]
        for s in cam_scenes:
            if calibrate.rect_present(maps, _parse(s["facecam_rect"])):
                return Layout(True, s.get("facecam_rect"), s.get("gameplay_rect"), "default-noface")
        # Not shown as a webcam any more (20 Nov, the bar is no wrong panel): a box around the judge's
        # point (the judge says where a webcam is, not where it ends) and, for a streamer known to have a
        # cam, a rectangle that looks like one with no face seen in it even enlarged.
        if cam_hint:
            return _unsure(
                ["the judge sees a webcam that the picture does not show a rectangle of"], 0.3, None
            )
        if cam_scenes and any(1.2 <= r.rect[2] / r.rect[3] <= 1.9 for r in calibrate.cam_proposals(maps)):
            why = "a rectangle that looks like a webcam, with no face seen in it and not the confirmed one"
            return _unsure([why], 0.3, None)
        no_cam = next((s for s in sc if s.get("has_webcam") is False), None)
        return Layout(False, None, no_cam.get("gameplay_rect") if no_cam else None, "no-face")
    # A face with nothing around it that says overlay. The camera is surely the picture when the picture
    # stays still around a face that is there (someone sitting in a room), or when several faces are
    # there together (people in front of one camera). One face in a picture that moves is a webcam with
    # no rectangle to be read, a cut-out, a handheld camera or a face of the game: not sure.
    here = seen(face) >= CUTOUT_SHARE * len(frames)
    if held is not None and (here or steady):
        x, y, w, h = held
        return _unsure([f"a rectangle around the face could be a webcam ({w}x{h} at {x},{y})"], 0.4, None)
    if len(steady) >= 2 and any(_cutout_like(f, maps) for f in steady):
        # purgegamers: a cut-out streamer and the game's character were 'two people in front of a camera'
        why = "several faces with no rectangle, one of them placed like a cut-out webcam over a game"
        return _unsure([why], 0.3, None)
    if (here and calibrate.scene_still(maps)) or len(steady) >= 2:
        no_cam = match_scene(face, True, sc)
        if no_cam and no_cam.get("gameplay_rect"):  # the operator's scene without a cam: its gameplay crop
            return Layout(False, None, no_cam.get("gameplay_rect"), f"scene:{no_cam.get('name', '?')}")
        return Layout(False, None, None, "fullcam", None, 0.9 if full_scene else 0.8)
    if here:
        why = ["one face, no rectangle around it, in a picture that moves: a webcam, a camera or the game?"]
        return _unsure(why, 0.3, None)
    return Layout(False, None, None, "no-face")


def _could_be_cam(maps, faces: list) -> tuple[int, int, int, int] | None:
    """The rectangle with sure sides around one of the faces that is a part of the picture (up to
    SPLIT_MAX of it), whatever else is known about it; None when there is none or it is the picture."""
    fw, fh = maps.size
    picture = maps.active[2] * maps.active[3] if maps.active else fw * fh
    for f in faces:
        if f[3] < 0.025 * fh:
            continue
        found = calibrate.cam_rect(maps, f)
        if found and CAM_MIN_AREA * fw * fh <= found[0][2] * found[0][3] <= SPLIT_MAX * picture:
            return found[0]
    return None


def _pick_cam(found: list[Cam], media: Path, fw: int, fh: int, peak_s, cam_hint, td: Path) -> Cam:
    """Of several webcams the one for the layout: the judge's (cam_hint); else the one whose face is
    there far more often than the others' (a video the streamer watches shows faces too, on and off);
    else, between cams of a size (a duo, a grid), whoever moves most at the moment. Cams of different
    sizes with nothing to tell the streamer's from one inside what is being watched: the largest, and
    doubts() says so."""
    if len(found) == 1:
        return found[0]
    if cam_hint is None:
        by_seen = sorted(found, key=lambda c: -c.seen)
        if by_seen[0].seen >= 1.5 * by_seen[1].seen:
            return by_seen[0]
        by_size = sorted(found, key=lambda c: -c.rect[2] * c.rect[3])
        if by_size[0].rect[2] * by_size[0].rect[3] >= SIZE_ALIKE * by_size[-1].rect[2] * by_size[-1].rect[3]:
            return by_size[0]
        if peak_s is None:
            return found[0]
    f = reacting_face(media, [c.face for c in found], fw, fh, peak_s, cam_hint, td)
    return next(c for c in found if c.face == f)


def _cutout_like(face, maps) -> bool:
    """A steady face without a rectangle around it is a cut-out webcam (green screen over the game) when
    it sits low in the picture, at a side or (in a picture with a HUD) anywhere along the bottom, is not
    larger than a webcam's, and what is right around the head moves like a game. Around a head in a room
    the wall stands still, however much the people move (yurnboi, easyallies: people at the sides of a
    full-screen camera were taken for cut-outs)."""
    import numpy as np

    fw, fh = maps.size
    x, y, w, h = (int(v) for v in face)
    cx = x + w / 2
    at_side = not 0.28 * fw < cx < 0.72 * fw
    if y + h / 2 < 0.5 * fh or h > CUTOUT_MAX_FACE * fh or not calibrate.face_alive(maps, face):
        return False
    if not at_side and calibrate.steady_share(maps) < GAME_STEADY:
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
