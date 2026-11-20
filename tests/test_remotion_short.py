"""render_short with a Remotion caption template, end to end (markers ffmpeg and remotion)."""

from __future__ import annotations

import pytest

from clipwatcher.render import captions, template
from clipwatcher.render import remotion as rm


def _numpy():
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def render_test(fn):
    """A test that renders: marked `remotion`, skipped without the Node setup or numpy."""
    fn = pytest.mark.skipif(not _numpy(), reason="numpy not installed")(fn)
    fn = pytest.mark.skipif(rm.missing() is not None, reason=f"Remotion not set up: {rm.missing()}")(fn)
    return pytest.mark.remotion(fn)


@pytest.mark.ffmpeg
@render_test
@pytest.mark.parametrize("colors", ["fixed", "auto"])
def test_render_short_with_a_remotion_template(colors, synth_src, tmp_path, monkeypatch):
    """The whole path: plan, (auto: sample the picture), render the band, composite. The captions land in
    the caption box of the Short and nowhere else, and the clip is otherwise the one ASS would give."""
    import subprocess

    import numpy as np

    from clipwatcher.config import ffmpeg_bin

    monkeypatch.setattr(rm, "color_mode", lambda: colors)
    words = captions.demo_words(4.0)
    made = {}
    for key, ws in (("with", words), ("without", [])):
        made[key] = template.render_short(
            synth_src, 8.0, 4.0, tmp_path / f"{key}.mp4", clip_id="clip-1", streamer="tester", words=ws,
            hook=None,
            profile="cpu_live", captions="rm:punch", layout="split",
        )  # fmt: skip

    def frame(p, t):
        r = subprocess.run(
            [ffmpeg_bin(), "-v", "error", "-ss", f"{t}", "-i", str(p), "-frames:v", "1", "-f", "rawvideo"]
            + ["-pix_fmt", "gray", "-"],
            capture_output=True, check=True,
        )  # fmt: skip
        return np.frombuffer(r.stdout, np.uint8).reshape(1920, 1080).astype(np.int16)

    box = rm.caption_box(template.SEAM_Y, "seam")
    seen = 0
    for t in (1.0, 2.0, 3.0):
        d = np.abs(frame(made["with"], t) - frame(made["without"], t)) > 48
        seen += int(d[box.y0 : box.y1, box.x0 : box.x1].sum())
        outside = d.copy()
        outside[box.y0 - 8 : box.y1 + 8, :] = False
        assert int(outside.sum()) < 400, (
            f"t={t}: the captions changed {int(outside.sum())} pixels outside their box"
        )
    assert seen > 20_000  # there are captions in the box
