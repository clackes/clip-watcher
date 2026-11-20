"""The webcam's own rectangle, read from the picture (calibrate.cam_rect, layout.resolve).

Synthetic scenes only: a "game" of random rectangles that changes every frame, with still "webcam" pictures
drawn over it (a room with a line or two and a head that moves a little). Every test names the live stream
whose failure it guards against (20 Nov check: 60 channels, scripts/webcam_check.py).
"""

import random

import pytest

from clipwatcher import calibrate

np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

W, H = 960, 540


def draw_cam(d: ImageDraw.ImageDraw, rect, i: int, *, wall: bool = False, head=None) -> tuple:
    """A webcam picture: a room (shelf line, optionally the edge of a darker wall) and a moving head.
    Returns the head's box."""
    x, y, w, h = rect
    d.rectangle([x, y, x + w - 1, y + h - 1], fill=(92, 104, 120))
    if wall:  # a wall's edge behind the streamer: there along most of the height, not all of it
        d.rectangle([x, y + h // 10, x + w // 3, y + h - h // 10], fill=(60, 66, 80))
    d.line([x + w // 2, y + h // 5, x + w - 8, y + h // 5], fill=(150, 150, 140), width=2)  # a shelf
    hx, hy, hw, hh = head or (x + w // 2 - w // 10, y + h // 4, w // 5, h // 3)
    dx, dy = (i * 5) % 7 - 3, (i * 3) % 5 - 2
    d.ellipse([hx + dx, hy + dy, hx + hw + dx, hy + hh + dy], fill=(205, 170, 150))
    if hy + hh + dy < y + h - 1:  # shoulders, down to the cam's bottom
        d.rectangle([hx - hw // 2 + dx, hy + hh + dy, hx + hw + hw // 2 + dx, y + h - 1], fill=(40, 40, 46))
    return (hx, hy, hw, hh)


def scene(tmp_path, cams=(), *, n=10, game=True, bars=(), stills=(), wall=False, seed=3, heads=None):
    """n frames (PNG paths). game: random rectangles, new every frame; else a flat still picture.
    bars: (x, y, w, h) flat graphics drawn after the cams; stills: rectangles holding a still face."""
    rnd = random.Random(seed)
    out, faces = [], []
    for i in range(n):
        im = Image.new("RGB", (W, H), (30, 34, 40))
        d = ImageDraw.Draw(im)
        if game:
            d.rectangle([0, 0, W, H], fill=tuple(rnd.randrange(20, 235) for _ in range(3)))
            for _ in range(70):
                x0, y0 = rnd.randrange(-60, W), rnd.randrange(-60, H)
                col = tuple(rnd.randrange(0, 255) for _ in range(3))
                d.rectangle([x0, y0, x0 + rnd.randrange(30, 320), y0 + rnd.randrange(30, 240)], fill=col)
        faces = [draw_cam(d, c, i, wall=wall, head=(heads or {}).get(k)) for k, c in enumerate(cams)]
        for x, y, w, h in bars:
            d.rectangle([x, y, x + w - 1, y + h - 1], fill=(18, 20, 60))
            d.rectangle([x + 6, y + 4, x + w // 3, y + h - 5], fill=(240, 240, 240))
        for x, y, w, h in stills:  # a portrait in a HUD box: a face that never moves
            d.rectangle([x, y, x + w - 1, y + h - 1], fill=(120, 60, 60))
            d.ellipse([x + w // 3, y + h // 4, x + 2 * w // 3, y + 3 * h // 4], fill=(210, 180, 160))
        p = tmp_path / f"s{seed}_{i:02d}.png"
        im.save(p)
        out.append(p)
    return out, faces


def near(a, b, tol=2) -> bool:
    return all(abs(p - q) <= tol for p, q in zip(a, b, strict=True))


def resolve_on(monkeypatch, frames, detector, cfg=None, **kw):
    """layout.resolve with frames standing in for the recording's (no ffmpeg). For tests/test_layout.py:
    conftest stubs layout.resolve in every other module."""
    from clipwatcher import layout

    timed = [(float(i), p) for i, p in enumerate(frames)]
    monkeypatch.setattr(layout.config, "streamer", lambda n: cfg or {})
    monkeypatch.setattr(layout.calibrate, "scene_frames", lambda m, d, w=None, n=24: timed)
    monkeypatch.setattr(layout.calibrate, "frames_at", lambda m, d, ts, height=None: timed[:6])
    return layout.resolve(frames[0].parent / "x.ts", "x", detector=detector, **kw)


def parse(r: str) -> tuple:
    w, h, x, y = (int(v) for v in r.split(":"))
    return x, y, w, h


# --------------------------------------------------------------------------- the rectangle
def test_the_rectangle_is_the_webcams_not_a_box_around_the_face(tmp_path):
    """ishowspeed / tumblurr / shahzam: a box of 2.2 face heights showed two thirds of a 16:9 cam and
    enlarged it a third more than needed; with a big face (zackrawrr) it reached into the game."""
    cam = (0, 120, 250, 140)
    frames, (face,) = scene(tmp_path, [cam])
    maps = calibrate.scene_maps(frames)
    rect, sure = calibrate.cam_rect(maps, face)
    assert near(rect, cam) and sure > 0.5 and calibrate.is_overlay(rect, W, H)
    big_face = (cam[0] + 70, cam[1] + 30, 96, 114)  # a face that nearly fills the cam, chin cut by it
    frames, _ = scene(tmp_path, [cam], heads={0: big_face}, seed=4)
    assert near(calibrate.cam_rect(calibrate.scene_maps(frames), big_face)[0], cam)


def test_a_bar_under_the_cam_stays_out_and_a_wall_edge_inside_it_does_not_cut_it(tmp_path):
    """ishowspeed (counters above and below the cam) / infume (the edge of a dark wall a third into the
    cam was taken for the cam's side)."""
    cam = (600, 200, 300, 170)
    frames, (face,) = scene(tmp_path, [cam], bars=[(600, 370, 300, 26)], wall=True)
    rect, _ = calibrate.cam_rect(calibrate.scene_maps(frames), face)
    assert near(rect, cam)


def test_a_camera_that_is_the_picture_is_not_an_overlay(tmp_path):
    """nagzz / kaneljoseph: the frame's own sides are the only rectangle around a full-screen camera."""
    frames, _ = scene(tmp_path, [(0, 0, W, H)], game=False)
    maps = calibrate.scene_maps(frames)
    found = calibrate.cam_rect(maps, (430, 100, 110, 120))
    assert found is None or not calibrate.is_overlay(found[0], W, H)
    assert calibrate.scene_still(maps)


def test_a_portrait_in_a_hud_box_is_not_alive(tmp_path):
    """faide (Apex): a legend's portrait in a HUD rectangle: a face in a box, but one that never moves."""
    box = (30, 30, 150, 120)
    frames, (face,) = scene(tmp_path, [(600, 300, 250, 140)], stills=[box])
    maps = calibrate.scene_maps(frames)
    assert not calibrate.face_alive(maps, (30 + 50, 30 + 30, 50, 60))
    assert calibrate.face_alive(maps, face)


def test_rect_present_tells_a_stale_confirmed_rect(tmp_path):
    cam = (0, 120, 250, 140)
    frames, _ = scene(tmp_path, [cam])
    maps = calibrate.scene_maps(frames)
    assert calibrate.rect_present(maps, cam)
    assert not calibrate.rect_present(maps, (0, 300, 250, 140))


# --------------------------------------------------------------------------- gameplay crop, scenes
def test_gameplay_crop_is_full_height_and_beside_the_cam():
    """Every sample before: the gameplay panel showed a 550x490 window of a 1920x1080 game."""
    assert calibrate.gameplay_crop([], 1920, 1080) == (352, 0, 1214, 1078)
    x, y, w, h = calibrate.gameplay_crop([(0, 162, 492, 413)], 1920, 1080)  # cam on the left edge
    assert y == 0 and h >= 1078 and x >= 492
    x, y, w, h = calibrate.gameplay_crop([(1400, 800, 520, 280)], 1920, 1080)  # bottom-right corner
    assert y == 0 and h >= 1078 and x + w <= 1400
    # cams on both edges: the window between them, a little smaller, rather than a cam in the game
    x, y, w, h = calibrate.gameplay_crop([(0, 626, 422, 238), (1498, 626, 422, 238)], 1920, 1080)
    assert x >= 422 and x + w <= 1498 and abs(w / h - 1.125) < 0.01
    # a cam in the middle of the bottom edge cannot be avoided at a useful size: full height, cam and all
    assert calibrate.gameplay_crop([(700, 700, 520, 380)], 1920, 1080)[3] >= 1078
    # the game's own letterbox stays out
    x, y, w, h = calibrate.gameplay_crop([], 1920, 1080, active=(0, 60, 1920, 960))
    assert y >= 60 and y + h <= 1020 and abs(w / h - 1.125) < 0.01


def test_black_bars_that_stay_are_not_picture(tmp_path):
    frames, _ = scene(tmp_path, [(0, 200, 200, 112)])
    for p in frames:  # a cutscene's letterbox
        im = Image.open(p)
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, W, 29], fill=(0, 0, 0))
        d.rectangle([0, H - 30, W, H], fill=(0, 0, 0))
        im.save(p)
    assert calibrate.scene_maps(frames).active == (0, 30, W, H - 60)
    plain, _ = scene(tmp_path, [(0, 200, 200, 112)], seed=8)
    assert calibrate.scene_maps(plain).active == (0, 0, W, H)


def test_frames_of_another_layout_are_left_out(tmp_path):
    """grandpoobear: four layouts in 84 s (one camera full screen, a 2x2 grid, a title card, a column of
    cams). Edges of two layouts in one map are the edges of neither."""
    a, _ = scene(tmp_path, [(0, 0, 300, 170)], n=8, seed=1)
    b, _ = scene(tmp_path, [(600, 300, 300, 170)], n=8, seed=2)
    timed = [(float(i), p) for i, p in enumerate(a + b)]
    assert calibrate.same_scene(timed, 12.0) == timed[8:]
    assert calibrate.same_scene(timed, 3.0) == timed[:8]
    assert calibrate.same_scene(timed[:8], 3.0) == timed[:8]  # nothing changes: all frames


def test_zoom_faces_maps_the_face_back(tmp_path):
    frames, _ = scene(tmp_path, [(100, 100, 200, 112)], n=4)
    seen = []

    def detector(p):
        with Image.open(p) as im:
            seen.append(im.size)
        return [(im.size[0] * 0.5, im.size[1] * 0.25, im.size[0] * 0.2, im.size[1] * 0.4)]

    boxes = calibrate.zoom_faces(frames, detector, (100, 100, 200, 112))
    assert len(boxes) == 4 and seen[0][1] >= 380  # enlarged
    x, y, w, h = boxes[0]
    assert near((x, y, w, h), (200, 128, 40, 45), tol=1)


def test_cutout_box_has_the_panels_shape_and_stays_in_the_frame():
    x, y, w, h = calibrate.cutout_box((1600, 640, 250, 250), 1920, 1080)
    assert x + w <= 1920 and y + h <= 1080 and abs(w / h - 1.125) < 0.01
    assert x <= 1600 and y <= 640 and x + w >= 1850 and y + h >= 890
