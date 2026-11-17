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


def test_confirmed_no_webcam_streamer_skips_face_detection(monkeypatch, tmp_path):
    """Regression: a GTA character's face was taken for a webcam on a no-cam Kick stream."""
    monkeypatch.setattr(layout.config, "streamer", lambda n: {"has_webcam": False})

    def boom(*a, **k):
        raise AssertionError("must not run detection")

    lay = layout.resolve(tmp_path / "x.mp4", "x", detector=boom)
    assert not lay.has_webcam and lay.source == "fixed"
