import pytest
import yaml

from clipwatcher import calibrate as cal

W, H = 1920, 1080


def _rect_overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _check(r):
    assert r.gameplay[2] == 2 * r.gameplay[3]
    for q in (r.facecam, r.gameplay):
        if q:
            x, y, w, h = q
            assert x >= 0 and y >= 0 and x + w <= W and y + h <= H
    if r.facecam:
        assert abs(r.facecam[2] / r.facecam[3] - 2.25) < 0.05
        assert not _rect_overlap(r.facecam, r.gameplay)


def test_face_left_bottom_overlay():
    boxes = [(80 + i, 760, 120, 150) for i in range(8)] + [(900, 300, 90, 90)]  # one spurious hit
    r = cal.compute_rects(boxes, W, H, 10)
    _check(r)
    x, y, w, h = r.facecam
    assert x == 0 and y + h == H  # snapped into the bottom-left corner
    assert r.face == (84, 760, 120, 150) or r.face[1] == 760


def test_face_right_top_overlay():
    boxes = [(1650, 40, 130, 160)] * 6
    r = cal.compute_rects(boxes, W, H, 10)
    _check(r)
    x, y, w, h = r.facecam
    assert x + w == W and y == 0


def test_no_face():
    r = cal.compute_rects([], W, H, 10)
    assert r.facecam is None and r.face is None
    assert r.gameplay == (0, 60, 1920, 960)
    _check(r)


def test_unstable_detections_ignored():
    boxes = [(100 * i, 100, 80, 80) for i in range(10)]  # every frame a different place
    assert cal.compute_rects(boxes, W, H, 10).facecam is None


def test_rect_str():
    assert cal.rect_str((0, 500, 520, 231)) == "520:231:0:500"


def test_update_streamer_rects_preserves(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(
        'streamers:\n  zack:\n    platform: twitch  # keep\n    facecam_rect: "1:1:0:0"  # old\n'
        "    keywords: {}\n  other:\n    platform: kick\n"
    )
    cal.update_streamer_rects(
        p, "zack", {"facecam_rect": "520:231:0:500", "gameplay_rect": "1380:690:540:250"}
    )
    txt = p.read_text()
    assert "# keep" in txt
    d = yaml.safe_load(txt)["streamers"]
    assert d["zack"]["facecam_rect"] == "520:231:0:500"
    assert d["zack"]["gameplay_rect"] == "1380:690:540:250"
    assert d["zack"]["keywords"] == {} and d["other"] == {"platform": "kick"}
    cal.update_streamer_rects(p, "newone", {"gameplay_rect": "2:1:0:0"})
    assert yaml.safe_load(p.read_text())["streamers"]["newone"]["gameplay_rect"] == "2:1:0:0"


def test_no_detector_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "make_detector", lambda *a, **k: None)
    res = cal.calibrate(tmp_path / "x.ts", "s", out_dir=tmp_path)
    assert res.rects is None and "detector" in res.message


@pytest.mark.ffmpeg
def test_calibrate_end_to_end_fake_detector(synth_src, tmp_path):
    # the synthetic source has a magenta "webcam" box at 40,700 560x250
    def fake(_p):
        return [(240.0, 720.0, 110.0, 140.0)]

    res = cal.calibrate(synth_src, "teststreamer", detector=fake, out_dir=tmp_path, n_frames=4)
    assert res.rects and res.rects.facecam
    _check(res.rects)
    assert res.still and res.still.exists()
    assert res.preview_9x16 is None or res.preview_9x16.exists()
