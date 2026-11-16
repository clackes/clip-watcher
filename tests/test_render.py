from __future__ import annotations

import json
import re
import subprocess

import pytest

from clipwatcher.config import ffmpeg_bin, ffprobe_bin
from clipwatcher.render.branding import hook_pill
from clipwatcher.render.captions import build_ass, demo_words, esc
from clipwatcher.render.encoders import resolve, video_args
from clipwatcher.render.template import (
    esc_filter_path,
    pick_style,
    render_clean,
    render_short,
)

from .conftest import SRC_SECONDS, second_colour


def probe(p):
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-count_frames", "-show_entries",
         "stream=codec_type,width,height,pix_fmt,nb_read_frames,avg_frame_rate", "-show_entries",
         "format=duration", "-of", "json", str(p)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(r.stdout)


def loudness(p) -> float:
    r = subprocess.run(
        [ffmpeg_bin(), "-nostats", "-i", str(p), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True,
    )  # fmt: skip
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)[-1])


def first_pixel(p, x=100, y=100) -> tuple[int, int, int]:
    vf = f"select=eq(n\\,6),crop=8:8:{x}:{y},format=rgb24"
    r = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-i", str(p), "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )  # fmt: skip
    return tuple(r.stdout[:3])


def check_short(out, dur, fps=60):
    info = probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["pix_fmt"]) == (1080, 1920, "yuv420p")
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - dur) <= 1 / fps + 0.03
    assert int(v["nb_read_frames"]) == round(dur * fps)
    assert abs(loudness(out) - -14) <= 1.0


def test_pure_helpers():
    assert pick_style("abc") == pick_style("abc")
    assert esc_filter_path("/a b/c:d'e") == "/a b/c\\:d\\'e"
    ass = build_ass(demo_words(4), 4, credit="twitch.tv/x")
    assert "PlayResX: 1080" in ass and "\\pos(540,1350)" in ass and "&H00E6FF&" in ass
    assert "\\an9\\pos(1060,282)" in ass
    assert "{" not in esc("a{b}\\c")
    pill = hook_pill("x" * 200)
    assert pill.size[1] == 96 and pill.size[0] <= 1000
    assert resolve("nope") == "cpu_batch" and "libx264" in video_args("cpu_live")


@pytest.mark.ffmpeg
@pytest.mark.parametrize("style,webcam", [("ambient", True), ("gradient", True), ("ambient", False)])
def test_short(synth_src, tmp_path, style, webcam):
    out = tmp_path / "s.mp4"
    render_short(
        synth_src, 8.0, 3.0, out, clip_id="t", streamer="tester", words=demo_words(3),
        hook="HE DID NOT SEE THAT", style=style, accent="cyan", has_webcam=webcam, profile="cpu_live",
    )  # fmt: skip
    check_short(out, 3.0)


@pytest.mark.ffmpeg
def test_short_cpu_batch_social(synth_src, tmp_path):
    out = tmp_path / "b.mp4"
    render_short(
        synth_src, 2.0, 3.0, out, clip_id="t2", streamer="tester", words=[], hook=None,
        platform="social", profile="cpu_batch",
    )  # fmt: skip
    check_short(out, 3.0)


@pytest.mark.ffmpeg
def test_clean(synth_src, tmp_path):
    out = tmp_path / "c.mp4"
    render_clean(synth_src, 5.0, 3.0, out, profile="cpu_live")
    info = probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1920, 1080)
    assert abs(loudness(out) - -14) <= 1.0


@pytest.mark.ffmpeg
@pytest.mark.parametrize("start", [7.0, 9.5, 13.0, 16.0])
def test_d9_ts_seek_lands_on_requested_second(synth_src, tmp_path, start):
    """Keyframes are 4 s apart: a naive exact -ss on .ts would land on the wrong second.

    Frame 6 (0.1 s in) is sampled because the synthetic .ts video starts ~1.3 frames after its audio.
    """
    assert start + 2 <= SRC_SECONDS
    out = tmp_path / "d9.mp4"
    render_clean(synth_src, start, 2.0, out, profile="cpu_live")
    got = first_pixel(out)
    k = int(start)
    best = min(
        range(SRC_SECONDS), key=lambda i: sum(abs(a - b) for a, b in zip(second_colour(i), got, strict=False))
    )
    assert best == k, (got, second_colour(k))
