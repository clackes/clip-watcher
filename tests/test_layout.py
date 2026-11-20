import pytest

from clipwatcher import layout

from .test_ts_readers import second_shown
from .test_webcam_rect import H, W, near, parse, resolve_on, scene

SCENES = [
    {"name": "gameplay", "facecam_rect": "520:231:0:500", "gameplay_rect": "1380:690:540:250"},
    {"name": "chatting", "has_webcam": False},
]


def test_face_inside_confirmed_cam_picks_that_scene():
    assert layout.match_scene((100, 560, 80, 90), False, SCENES)["name"] == "gameplay"


def test_face_elsewhere_is_not_matched():
    assert layout.match_scene((1500, 100, 80, 90), False, SCENES) is None


def test_full_camera_or_no_face_uses_no_cam_scene():
    assert layout.match_scene((600, 200, 500, 500), True, SCENES)["name"] == "chatting"
    assert layout.match_scene(None, False, SCENES)["name"] == "chatting"
    assert layout.match_scene(None, False, SCENES[:1]) is None


def test_streamer_level_rect_counts_as_a_scene():
    sc = layout.scenes({"facecam_rect": "520:231:0:500", "gameplay_rect": "1:1:0:0"})
    assert sc[0]["has_webcam"] and sc[0]["facecam_rect"] == "520:231:0:500"
    assert layout.scenes({"has_webcam": False})[0]["has_webcam"] is False


def test_resolve_new_scene_is_flagged(monkeypatch, tmp_path):
    """A webcam nobody confirmed: rendered from its own rectangle, flagged auto-new for the operator."""
    cam = (700, 40, 240, 136)
    frames, (face,) = scene(tmp_path, [cam])
    lay = resolve_on(monkeypatch, frames, lambda p: [face])
    assert lay.source == "auto-new" and lay.has_webcam and near(parse(lay.cam), cam)
    lay = resolve_on(monkeypatch, frames, lambda p: [])
    assert lay.source == "no-face" and not lay.has_webcam
    # Regression: a missed face on a streamer with a confirmed cam scene kept the full frame (tfue).
    cfg = {"has_webcam": True, "facecam_rect": "240:136:700:40", "gameplay_rect": "712:356:0:92"}
    lay = resolve_on(monkeypatch, frames, lambda p: [], cfg)
    assert lay.source == "default-noface" and lay.has_webcam and lay.cam == "240:136:700:40"


def test_no_way_to_read_rectangles_is_no_cam_panel(monkeypatch, tmp_path):
    """Too few frames (or no numpy) to read the webcam's rectangle: a box guessed around a face showed the
    wrong thing in two thirds of the 20 Nov samples, and a confirmed rect nobody can check against the
    picture was stale four times of four. The whole frame instead; `layout: fixed` is the way to insist."""
    from PIL import Image

    frame = tmp_path / "f.png"
    Image.new("RGB", (1920, 1080)).save(frame)
    monkeypatch.setattr(layout.config, "streamer", lambda n: {})
    monkeypatch.setattr(layout.calibrate, "scene_frames", lambda m, d, w=None, n=24: [])
    monkeypatch.setattr(
        layout.calibrate, "sample_frames", lambda m, d, n, window=None: [(float(i), frame) for i in range(n)]
    )
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: [(1700.0, 100.0, 90.0, 100.0)])
    assert lay.source == "unsure" and not lay.has_webcam and lay.doubts
    assert not lay.as_render_kwargs()["reframe"]
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: [])
    assert lay.source == "no-face" and not lay.has_webcam
    monkeypatch.setattr(
        layout.config, "streamer",
        lambda n: {"has_webcam": True, "facecam_rect": "494:220:0:180", "gameplay_rect": "1426:712:494:184"},
    )  # fmt: skip
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: [(100.0, 240.0, 90.0, 100.0)])
    assert lay.source == "unsure" and not lay.has_webcam


def test_fixed_layout_skips_detection(monkeypatch, tmp_path):
    """Regression: a GTA character's face was taken for a webcam on a no-cam Kick stream. `layout: fixed`
    is the operator's word against the picture; has_webcam: false alone no longer is (kaicenat had a cam
    on 20 Nov), it only keeps a bare face from becoming one
    (test_has_webcam_false_does_not_stop_looking)."""
    monkeypatch.setattr(layout.config, "streamer", lambda n: {"has_webcam": False, "layout": "fixed"})

    def boom(*a, **k):
        raise AssertionError("must not run detection")

    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=boom)
    assert not lay.has_webcam and lay.source == "fixed"
    cfg = {"layout": "fixed", "facecam_rect": "494:220:0:180", "gameplay_rect": "1426:712:494:184"}
    monkeypatch.setattr(layout.config, "streamer", lambda n: cfg)
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=boom)
    assert lay.has_webcam and lay.cam == "494:220:0:180" and lay.source == "fixed"


def test_two_webcams_pick_the_one_reacting_at_the_peak(monkeypatch, tmp_path):
    """Duo stream: the cam in the layout is the one of whoever makes the moment, not the bigger cluster."""
    left, right = (0, 300, 220, 124), (740, 300, 220, 124)
    frames, faces = scene(tmp_path, [left, right])
    seen = {}

    def moving(media, fs, fw, fh, peak, hint, td):  # stands in for the motion measure at the peak
        seen["faces"] = len(fs)
        return max(fs, key=lambda f: f[0])  # the right-hand one

    monkeypatch.setattr(layout, "reacting_face", moving)
    lay = resolve_on(monkeypatch, frames, lambda p: faces, peak_s=5.0)
    assert lay.source == "multi-cam" and seen["faces"] == 2 and near(parse(lay.cam), right)
    gx, _gy, gw, _gh = parse(lay.game)
    assert gx >= left[2] and gx + gw <= right[0]  # the gameplay between the two cams (owner, 17 Nov)
    # without a peak or a hint: the most seen
    assert resolve_on(monkeypatch, frames, lambda p: faces).source == "multi-cam"


def test_still_object_next_to_the_face_is_not_the_cam(monkeypatch, tmp_path):
    """Regression (officermesser): vests on the wall next to the face were taken for a second cam."""
    from PIL import Image, ImageDraw

    real, vests = (241.0, 145.0, 88.0, 88.0), (408.0, 102.0, 116.0, 116.0)
    peak = []
    for i in range(6):  # the face changes every frame, the vests never do
        q = tmp_path / f"p{i}.png"
        im = Image.new("RGB", (1920, 1080), (90, 90, 90))
        ImageDraw.Draw(im).rectangle([250, 150, 320, 220], fill=(255, 255, 255) if i % 2 else (0, 0, 0))
        im.save(q)
        peak.append((float(i), q))
    monkeypatch.setattr(layout.calibrate, "frames_at", lambda m, d, ts, height=None: peak)
    assert layout.reacting_face(tmp_path / "x.mp4", [vests, real], 1920, 1080, 10.0, None, tmp_path) == real


def test_judge_hint_picks_the_cam(tmp_path):
    """The judge sees who makes the moment: its cam point beats motion (ishowspeed, stacked cams)."""
    speed, guest = (122.0, 428.0, 105.0, 105.0), (120.0, 592.0, 100.0, 100.0)
    right = (1667.0, 624.0, 110.0, 110.0)
    hint = (0.08, 0.44)  # centre of the top-left cam
    assert (
        layout.reacting_face(tmp_path / "x", [right, guest, speed], 1920, 1080, None, hint, tmp_path) == speed
    )
    far = (0.5, 0.5)  # nowhere near a face: falls back (no peak -> first face)
    assert layout.reacting_face(tmp_path / "x", [right, guest], 1920, 1080, None, far, tmp_path) == right


def test_stacked_small_cams_are_separate():
    """Regression (ishowspeed Rocket League): two cams stacked 160 px apart were merged into one."""
    from clipwatcher import calibrate

    boxes = [(122, 428, 107, 107), (129, 594, 101, 101), (122, 425, 103, 103), (124, 591, 98, 98)]
    assert len(calibrate.stable_faces(boxes, 4, 1920, 1080)) == 2


def test_gameplay_leaves_out_every_cam():
    """Multi-cam: the gameplay crop sits between the cam columns (owner: other cams need not be shown)."""
    from clipwatcher import calibrate

    cams = [(0, 380, 330, 146), (0, 540, 330, 146), (1600, 560, 320, 142), (1640, 400, 280, 124)]
    x, y, w, h = calibrate.gameplay_between(cams, 1920, 1080)
    assert x >= 330 and x + w <= 1600 and w == 2 * h
    assert calibrate.gameplay_between([(0, 0, 900, 400), (1000, 0, 900, 400)], 1920, 1080) is None


@pytest.mark.ffmpeg
def test_resolve_looks_at_the_moment_not_at_the_whole_recording(synth_src, monkeypatch):
    """With the VOD lane's --video the media is the whole VOD. The frames that decide the layout must come
    from around the moment: here the streamer changes scene at second 12 and the moment is at second 18."""
    early = {"name": "early", "facecam_rect": "520:231:0:780", "gameplay_rect": "1380:690:540:0"}
    late = {"name": "late", "facecam_rect": "520:231:1400:0", "gameplay_rect": "1380:690:0:300"}
    monkeypatch.setattr(layout.config, "streamer", lambda n: {"layouts": [early, late]})
    seen = []

    def detector(p):  # the webcam face sits bottom-left until second 12, top-right after
        seen.append(second_shown(p))
        return [(1700.0, 100.0, 90.0, 100.0)] if seen[-1] >= 12 else [(100.0, 850.0, 90.0, 100.0)]

    lay = layout.resolve(synth_src, "x", detector=detector, peak_s=18.0)
    # the face is where 'late' has its cam; the test picture has no such rectangle, so no cam panel
    assert lay.source == "unsure" and "'late'" in lay.doubts[0]
    assert len(seen) == 4
    assert all(18.0 - layout.SCENE_BEFORE_S <= k <= 18.0 + layout.SCENE_AFTER_S for k in seen)
    seen.clear()  # no moment given (calibration): the whole media, as before
    layout.resolve(synth_src, "x", detector=detector)
    assert len(seen) == 4 and min(seen) < 5 and max(seen) > 15


# --------------------------------------------------------------------------- the 20 Nov live check
def test_a_camera_that_is_the_picture_is_full_camera_not_cams(tmp_path, monkeypatch):
    """nagzz / kaneljoseph / yurnboi: a full-screen camera (one person or several) became 'multi-cam'
    with a tight crop of one face on top and the same room below."""
    frames, _ = scene(tmp_path, [(0, 0, W, H)], game=False)
    people = [(200, 150, 60, 66), (460, 140, 62, 70), (700, 160, 58, 64)]
    lay = resolve_on(monkeypatch, frames, lambda p: people, peak_s=5.0)
    assert not lay.has_webcam and lay.source == "fullcam" and lay.as_render_kwargs()["reframe"]


def test_a_portrait_in_a_hud_box_is_not_a_webcam(tmp_path, monkeypatch):
    """faide (Apex): faces 'seen' now and then inside a HUD rectangle whose inside never rests."""
    box = (30, 30, 150, 120)
    frames, _ = scene(tmp_path, stills=[box])
    lay = resolve_on(monkeypatch, frames, lambda p: [(30 + 50, 30 + 30, 50, 60)], peak_s=5.0)
    assert not lay.has_webcam


def test_resolve_uses_the_rectangle_in_the_picture_not_the_confirmed_one(tmp_path, monkeypatch):
    """zackrawrr, clix, tfue, ishowspeed on 20 Nov: every confirmed facecam_rect was somewhere else than
    the cam that day. A face found: the cam's own rectangle. No face found (tfue sits in profile): the
    stale rect showed the bottom of the cam and a strip of game. A rectangle that looks like a cam with
    no face seen in it is not shown as one either (round two): the whole frame, until the operator
    confirms the rectangle that is there."""
    cam = (0, 0, 220, 124)
    stale = {"has_webcam": True, "facecam_rect": "248:110:0:90", "gameplay_rect": "712:356:248:92"}
    frames, (face,) = scene(tmp_path, [cam])
    lay = resolve_on(monkeypatch, frames, lambda p: [face], stale, peak_s=5.0)
    assert lay.has_webcam and lay.source == "auto-new" and near(parse(lay.cam), cam)
    lay = resolve_on(monkeypatch, frames, lambda p: [], stale, peak_s=5.0)
    assert lay.source == "unsure" and not lay.has_webcam and not lay.as_render_kwargs()["reframe"]
    # a confirmed rect the picture still shows keeps its scene name and its gameplay_rect
    right = {"has_webcam": True, "facecam_rect": "220:124:0:0", "gameplay_rect": "712:356:248:92"}
    lay = resolve_on(monkeypatch, frames, lambda p: [face], right, peak_s=5.0)
    assert lay.source == "scene:default" and lay.game == right["gameplay_rect"]
    lay = resolve_on(monkeypatch, frames, lambda p: [], right, peak_s=5.0)
    assert lay.source == "default-noface" and lay.cam == right["facecam_rect"]


def test_has_webcam_false_does_not_stop_looking(tmp_path, monkeypatch):
    """kaicenat: 'has_webcam: false' in streamers.yaml, a webcam on screen: every clip lost him. A face
    alone is still no cam for such a streamer (the GTA character the rule was written for)."""
    cam = (0, 216, 196, 110)
    frames, (face,) = scene(tmp_path, [cam])
    lay = resolve_on(monkeypatch, frames, lambda p: [face], {"has_webcam": False}, peak_s=5.0)
    assert lay.has_webcam and near(parse(lay.cam), cam)
    game_only, _ = scene(tmp_path, [], seed=9)
    character = (420, 160, 150, 160)
    lay = resolve_on(monkeypatch, game_only, lambda p: [character], {"has_webcam": False}, peak_s=5.0)
    assert not lay.has_webcam and lay.source == "fixed"


def test_a_face_of_the_game_is_not_taken_while_the_cams_own_face_is_found_by_looking_closer(
    tmp_path, monkeypatch
):
    """kaicenat / clix / tfue, GTA VI cutscenes: the character's close-up was the only face the full-frame
    pass saw (the cam's face is small and dark) and the Short became a 'full-screen camera' on the
    character. The rectangle that looks like a webcam is enlarged and searched: the face is there."""
    cam = (0, 216, 196, 110)
    frames, (face,) = scene(tmp_path, [cam])
    character = (420, 120, 170, 180)

    def detector(p):
        if p.name.startswith("zoom_"):  # the enlarged cam: its face is found (in the crop's own pixels)
            return [(250.0, 90.0, 140.0, 150.0)]
        return [character] if int(p.stem[-2:]) % 2 else []

    lay = resolve_on(monkeypatch, frames, detector, peak_s=5.0)
    assert lay.has_webcam and near(parse(lay.cam), cam)
    # and with no webcam at all a face that comes and goes is nobody's webcam
    game_only, _ = scene(tmp_path, [], seed=11)
    lay = resolve_on(monkeypatch, game_only, lambda p: [character] if int(p.stem[-2:]) % 3 == 0 else [])
    assert not lay.has_webcam


def test_the_streamers_cam_is_the_large_one_not_the_cam_inside_what_they_watch(tmp_path, monkeypatch):
    """coopertv / nattynattlol: a small webcam inside the watched stream moved more and was chosen. The
    judge's point says which is the streamer's; without it two rectangles of different sizes are not
    told apart from the picture (round two: hasanabi's was the video window, warn's the watched stream)."""
    own, other = (0, 0, 260, 180), (560, 300, 130, 74)
    frames, faces = scene(tmp_path, [own, other])
    lay = resolve_on(monkeypatch, frames, lambda p: faces, peak_s=5.0)
    assert lay.source == "unsure" and not lay.has_webcam and "several" in lay.doubts[0]
    lay = resolve_on(monkeypatch, frames, lambda p: faces, peak_s=5.0, cam_hint=(0.13, 0.17))
    assert lay.source == "multi-cam" and near(parse(lay.cam), own)
    gx, gy, gw, gh = parse(lay.game)
    assert gx >= own[0] + own[2] and gh >= 0.95 * H  # the gameplay crop: full height, beside the cam


def test_a_cut_out_streamer_over_the_game_gets_a_crop_around_them(tmp_path, monkeypatch):
    """trymacs / w33haa: green-screen cut-out, no rectangle. A big one was a 'full-screen camera'."""
    frames, _ = scene(tmp_path, [])
    person = (790, 300, 120, 130)  # in the corner, in every frame, the game moving around
    moving = iter(range(10_000))

    def detector(p):
        k = next(moving) % 3
        return [(person[0] + k, person[1] - k, person[2], person[3])]

    from PIL import Image, ImageDraw

    # the face itself has to change too: draw it
    for i, p in enumerate(frames):
        im = Image.open(p)
        d = ImageDraw.Draw(im)
        d.ellipse([person[0] + i % 5, person[1], person[0] + person[2] + i % 5, person[1] + person[3]],
                  fill=(200 - 9 * i, 170, 150))  # fmt: skip
        im.save(p)
    lay = resolve_on(monkeypatch, frames, detector, peak_s=5.0)
    assert lay.has_webcam and lay.source == "auto-new"
    x, y, w, h = parse(lay.cam)
    assert (
        x <= person[0]
        and x + w >= person[0] + person[2]
        and y <= person[1]
        and y + h >= person[1] + person[3]
    )
    assert abs(w / h - 1080 / 960) < 0.02 and x + w <= W and y + h <= H
