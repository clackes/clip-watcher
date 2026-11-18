from clipwatcher import layout

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
    from PIL import Image

    frame = tmp_path / "f.png"
    Image.new("RGB", (1920, 1080)).save(frame)
    monkeypatch.setattr(layout.config, "streamer", lambda n: {})
    monkeypatch.setattr(
        layout.calibrate, "sample_frames", lambda m, d, n: [(float(i), frame) for i in range(n)]
    )
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: [(1700.0, 100.0, 90.0, 100.0)])
    assert lay.source == "auto-new" and lay.has_webcam and lay.cam
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: [])
    assert lay.source == "no-face" and not lay.has_webcam
    # Regression: a missed face on a streamer with a confirmed cam scene kept the full frame (tfue).
    monkeypatch.setattr(
        layout.config, "streamer",
        lambda n: {"has_webcam": True, "facecam_rect": "494:220:0:180", "gameplay_rect": "1426:712:494:184"},
    )  # fmt: skip
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: [])
    assert lay.source == "default-noface" and lay.has_webcam and lay.cam == "494:220:0:180"


def test_confirmed_no_webcam_streamer_skips_face_detection(monkeypatch, tmp_path):
    """Regression: a GTA character's face was taken for a webcam on a no-cam Kick stream."""
    monkeypatch.setattr(layout.config, "streamer", lambda n: {"has_webcam": False})

    def boom(*a, **k):
        raise AssertionError("must not run detection")

    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=boom)
    assert not lay.has_webcam and lay.source == "fixed"


def test_two_webcams_pick_the_one_reacting_at_the_peak(monkeypatch, tmp_path):
    """Duo stream: the cam in the layout is the one of whoever makes the moment, not the bigger cluster."""
    from PIL import Image, ImageDraw

    still = tmp_path / "still.png"
    Image.new("RGB", (1920, 1080), (40, 40, 40)).save(still)
    peak = []
    for i in range(6):  # right face (1700,100) flickers around the peak, left face (100,600) stays still
        f = tmp_path / f"p{i}.png"
        im = Image.new("RGB", (1920, 1080), (40, 40, 40))
        ImageDraw.Draw(im).rectangle([1700, 100, 1790, 200], fill=(255, 255, 255) if i % 2 else (0, 0, 0))
        im.save(f)
        peak.append((float(i), f))
    monkeypatch.setattr(layout.config, "streamer", lambda n: {})
    monkeypatch.setattr(
        layout.calibrate, "sample_frames", lambda m, d, n: [(float(i), still) for i in range(n)]
    )
    monkeypatch.setattr(layout.calibrate, "frames_at", lambda m, d, ts, height=None: peak)
    faces = [(100.0, 600.0, 90.0, 100.0), (1700.0, 100.0, 90.0, 100.0)]
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: faces, peak_s=10.0)
    assert lay.source == "multi-cam"
    w, h, x, y = (int(v) for v in lay.cam.split(":"))
    assert x <= 1745 <= x + w and y <= 150 <= y + h  # the moving face's cam
    # without a peak the old behaviour: largest/first cluster
    assert layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: faces).source == "auto-new"


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


def test_judge_point_finds_a_small_cam_the_full_pass_missed(monkeypatch, tmp_path):
    """Regression (myth, kaicenat): small dark cams gave no-face and a Short without the streamer."""
    from PIL import Image

    frame = tmp_path / "f.png"
    Image.new("RGB", (1920, 1080)).save(frame)
    monkeypatch.setattr(layout.config, "streamer", lambda n: {})
    monkeypatch.setattr(
        layout.calibrate, "sample_frames", lambda m, d, n: [(float(i), frame) for i in range(n)]
    )

    def det(p):  # only the 2x zoom around the judge's point finds the face
        return [(200.0, 180.0, 80.0, 80.0)] if p.name.startswith("near_") else []

    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=det, cam_hint=(0.8, 0.5))
    assert lay.has_webcam and lay.source == "auto-new"
    w, h, x, y = (int(v) for v in lay.cam.split(":"))
    assert x < 1368 < x + w  # 2x-zoom box mapped back: 1248 + (200 + 40) / 2
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: [], cam_hint=(0.8, 0.5))
    assert lay.source == "judge-cam" and lay.has_webcam
    assert layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: []).source == "no-face"


def test_no_face_is_not_reframed_and_a_passing_closeup_is_not_a_full_cam(monkeypatch, tmp_path):
    """Owner, 18 Nov: gameplay without a webcam (GTA, avatars) got the IRL centre crop. Only a real
    full-screen camera (big face in nearly every frame) is reframed; a game character's close-up is not."""
    from PIL import Image

    frames = []
    for i in range(4):
        f = tmp_path / f"f{i}.png"
        Image.new("RGB", (1920, 1080)).save(f)
        frames.append((float(i), f))
    monkeypatch.setattr(layout.config, "streamer", lambda n: {})
    monkeypatch.setattr(layout.calibrate, "sample_frames", lambda m, d, n: frames[:n])
    big = (700.0, 200.0, 420.0, 420.0)  # a face ~39% of the frame height

    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: [])
    assert lay.source == "no-face" and not lay.as_render_kwargs()["reframe"]

    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: [big])
    assert lay.source == "fullcam" and lay.as_render_kwargs()["reframe"]

    closeup = {frames[0][1], frames[1][1]}  # the character's face in 2 of 4 frames only
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: [big] if p in closeup else [])
    assert lay.source == "no-face" and not lay.has_webcam and not lay.as_render_kwargs()["reframe"]
