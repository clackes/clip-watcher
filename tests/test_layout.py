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
    monkeypatch.setattr(layout.calibrate, "frames_at", lambda m, d, ts, height=360: peak)
    faces = [(100.0, 600.0, 90.0, 100.0), (1700.0, 100.0, 90.0, 100.0)]
    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: faces, peak_s=10.0)
    assert lay.source == "multi-cam"
    w, h, x, y = (int(v) for v in lay.cam.split(":"))
    assert x <= 1745 <= x + w and y <= 150 <= y + h  # the moving face's cam
    # without a peak the old behaviour: largest/first cluster
    assert layout.resolve(tmp_path / "x.mp4", "x", detector=lambda p: faces).source == "auto-new"


def test_second_face_inside_the_same_webcam_is_not_another_cam():
    """Regression (officermesser): a shelf object next to the streamer's face was taken for a second cam."""
    from clipwatcher import calibrate

    boxes = [(233, 144, 91, 91), (399, 85, 131, 131), (241, 141, 94, 94), (416, 119, 101, 101)]
    boxes += [(241, 146, 84, 84), (249, 151, 84, 84)]
    faces = calibrate.stable_faces(boxes, 4, 1920, 1080)
    assert len(faces) == 1 and faces[0][0] < 300
    tiny = [(1500, 900, 30, 30)] * 4  # ID-card photo in the HUD
    assert len(calibrate.stable_faces(boxes + tiny, 4, 1920, 1080)) == 1
