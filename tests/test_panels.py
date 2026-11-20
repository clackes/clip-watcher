"""The two panels of the split Short show crops of their own shape (render/template.py): never a rectangle
squeezed into the panel, never bars, the webcam on top and the game below."""

import pytest

np = pytest.importorskip("numpy")
from PIL import Image, ImageDraw  # noqa: E402


def test_panels_are_cropped_to_their_shape_never_stretched():
    """A webcam's own rectangle is 16:9 or 4:3, not the shape of a panel: the split layout takes the
    largest crop of the panel's shape inside it, around the face; the stack layout (kept as an option)
    scaled whatever rectangle it was given to 1080x480, which squeezed anything not 2.25:1."""
    import re
    from pathlib import Path

    from clipwatcher.render.template import build_short_graph, build_split_graph, split_crops

    cam, game, face = (0, 80, 478, 269), (706, 0, 1214, 1078), (330, 150, 110, 120)
    (cx, cy, cw, ch), (gx, gy, gw, gh) = split_crops(cam, game, face, (1920, 1080))
    assert abs(cw / ch - 1.125) < 0.01 and abs(gw / gh - 1.125) < 0.01
    assert cx >= 0 and cx + cw <= 478 and (cy, ch) == (80, 268)  # the cam's full height, inside the cam
    assert cx <= face[0] and face[0] + face[2] <= cx + cw  # the face is in it
    args = {"trim": 0.0, "dur": 5.0, "has_webcam": True, "cam": cam, "game": game, "ass": Path("s.ass"),
            "fontsdir": Path("fonts"), "face": face}  # fmt: skip
    split = build_split_graph(logo_idx=-1, **args)
    assert f"crop={cw}:{ch}:{cx}:{cy},scale=1080:960" in split
    stack = build_short_graph(
        style="ambient", has_hook=False, gradient_idx=-1, logo_idx=1, hook_idx=-1, **args
    )
    w, h = (int(v) for v in re.search(r"\[cs\]crop=(\d+):(\d+):", stack).groups())
    assert abs(w / h - 2.25) < 0.03
    w, h = (int(v) for v in re.search(r"\[gs\]crop=(\d+):(\d+):", stack).groups())
    assert abs(w / h - 2.0) < 0.02
    # a rectangle that reaches past the picture (a stale confirmed rect on a smaller source) is clipped
    (cx, cy, cw, ch), _ = split_crops((1700, 900, 500, 400), game, None, (1920, 1080))
    assert cx + cw <= 1920 and cy + ch <= 1080 and abs(cw / ch - 1.125) < 0.02


@pytest.mark.ffmpeg
def test_a_round_thing_in_the_webcam_stays_round_in_the_short(tmp_path):
    """End to end on a tiny video: a 16:9 webcam with a disc in it over a plain game. In the Short the
    cam panel shows the disc round (aspect kept), fills its panel (no black bars) and holds no game."""
    import subprocess

    from clipwatcher import config
    from clipwatcher.render.template import render_short

    frame = Image.new("RGB", (640, 360), (20, 120, 40))  # the game: green
    d = ImageDraw.Draw(frame)
    d.rectangle([0, 0, 319, 179], fill=(40, 60, 200))  # the webcam: blue, 320x180
    d.ellipse([115, 55, 205, 145], fill=(255, 0, 255))  # the disc, 90 px across, clear of the logo
    frame.save(tmp_path / "f.png")
    src = tmp_path / "src.ts"
    subprocess.run(
        [config.ffmpeg_bin(), "-y", "-loglevel", "error", "-loop", "1", "-framerate", "30", "-i",
         str(tmp_path / "f.png"), "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", str(src)],
        check=True,
    )  # fmt: skip
    out = render_short(
        src, 0.5, 2.0, tmp_path / "short.mp4", clip_id="disc", streamer="x", words=[], hook=None,
        cam=(0, 0, 320, 180), game=(320, 0, 320, 284), has_webcam=True, profile="cpu_live", layout="split",
    )  # fmt: skip
    subprocess.run(
        [config.ffmpeg_bin(), "-y", "-loglevel", "error", "-ss", "1", "-i", str(out), "-frames:v", "1",
         str(tmp_path / "o.png")],
        check=True,
    )  # fmt: skip
    a = np.asarray(Image.open(tmp_path / "o.png").convert("RGB")).astype(int)
    assert a.shape[:2] == (1920, 1080)
    cam, game = a[200:960], a[960:]  # under the logo
    disc = (cam[..., 0] > 180) & (cam[..., 1] < 90) & (cam[..., 2] > 180)
    ys, xs = np.nonzero(disc)
    wide, tall = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    assert abs(wide / tall - 1.0) < 0.01 and abs(wide - 90 * 960 / 180) < 8  # round, and 5.33 times larger
    blue = (cam[..., 2] > 140) & (cam[..., 0] < 110) & ~disc
    assert (blue | disc).mean() > 0.97  # nothing but webcam in the cam panel: no bars, no game
    assert (
        (game[..., 1] > 90) & (game[..., 0] < 80) & (game[..., 2] < 100)
    ).mean() > 0.97  # and no cam below
