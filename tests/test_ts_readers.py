"""D9 for everything that reads only the picture or only the sound of a .ts.

The fixtures are .ts files whose audio and video start 0.7 s apart, as live recordings do by a little.
Time is the file's own clock, the one a render with both streams uses: 0 = the first sample of the stream
that starts first. On that clock the box in the corner has the colour of the running second and a beep
starts at 3.0 s and at 12.0 s. Each reader is asked for a moment and must return that moment.
"""

from __future__ import annotations

import array
import subprocess
import wave

import pytest
from PIL import Image

from clipwatcher.config import ffmpeg_bin

from .conftest import second_colour

pytestmark = pytest.mark.ffmpeg

SECONDS = 20
OFFSET_S = 0.7  # how much later the other stream starts
BEEPS = (3.0, 12.0)


def _build(path, first: str) -> None:
    v0, a0 = (OFFSET_S, 0.0) if first == "audio" else (0.0, OFFSET_S)
    boxes = []
    for k in range(SECONDS):
        r, g, b = second_colour(k)
        boxes.append(
            f"drawbox=x=0:y=0:w=160:h=120:color=0x{r:02x}{g:02x}{b:02x}:t=fill"
            f":enable='between(t+{v0},{k},{k + 1})'"
        )
    beeps = "+".join(f"between(t+{a0},{b},{b + 0.2})" for b in BEEPS)
    graph = (
        f"[0:v]{','.join(boxes)},setpts=PTS+{v0}/TB[v];"  # each stream keeps its own late start
        f"[1:a]asetpts=PTS+{a0}*SR[a]"
    )
    cmd = [
        ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=gray:s=640x360:r=30:d={SECONDS - v0}",
        "-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*1000*t)*({beeps})':s=48000:d={SECONDS - a0}",
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", "120", "-keyint_min", "120", "-sc_threshold", "0",  # keyframes 4 s apart
        "-c:a", "aac", "-b:a", "128k", "-f", "mpegts", str(path),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)


@pytest.fixture(scope="module", params=["audio", "video"])
def ts(request, tmp_path_factory):
    """A .ts in which the named stream starts OFFSET_S before the other."""
    p = tmp_path_factory.mktemp("ts") / f"{request.param}_first.ts"
    _build(p, request.param)
    return p


def second_shown(img) -> int:
    """The second whose colour fills the corner box of a frame."""
    with Image.open(img) as im:
        got = im.convert("RGB").getpixel((im.width // 20, im.height // 10))

    def off(k: int) -> int:
        return sum(abs(a - b) for a, b in zip(second_colour(k), got, strict=True))

    return min(range(SECONDS), key=off)


def beep_onset(wav) -> float | None:
    with wave.open(str(wav)) as w:
        rate, pcm = w.getframerate(), array.array("h", w.readframes(w.getnframes()))
    return next((i / rate for i, v in enumerate(pcm) if abs(v) > 3000), None)


def test_fixture_streams_start_apart(ts):
    from clipwatcher.config import ffprobe_bin

    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "stream=codec_type,start_time", "-of",
         "csv=p=0", str(ts)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    start = {kind: float(t) for kind, t in (ln.split(",") for ln in r.stdout.split())}
    late, early = ("video", "audio") if ts.name.startswith("audio") else ("audio", "video")
    assert 0.6 <= start[late] - start[early] <= 0.8


@pytest.mark.parametrize("start", [1.0, 9.0, 14.0])
def test_judge_frames_show_the_second_they_are_labelled_with(ts, tmp_path, start):
    from clipwatcher.judge import frames

    low = frames.keyframes_lowres(ts, start, 4.0, n=4, out_dir=tmp_path / "low")
    assert [t for t, _ in low] == [0.5, 1.5, 2.5, 3.5]
    assert [second_shown(p) for _, p in low] == [int(start + t) for t, _ in low]
    full = frames.keyframes(ts, start, 4.0, n=4, height=180, out_dir=tmp_path / "full")
    assert [second_shown(p) for p in full] == [int(start + t) for t in (0.5, 1.5, 2.5, 3.5)]


@pytest.mark.parametrize("start", [1.05, 9.05])
def test_reframe_samples_the_clip_it_was_given(ts, start):
    """A sample shows the picture up to 0.25 s after its label (fps=2 keeps the last frame of each slot),
    so the clip starts just after a whole second and every sample stays inside its second."""
    from clipwatcher.render import reframe

    def detector(p):  # a "face" whose position tells which second the frame shows
        with Image.open(p) as im:
            w, h = im.size
        return [((second_shown(p) + 0.5) / SECONDS * w - 20, 0.3 * h, 40.0, 0.3 * h)]

    got = reframe.track(ts, start, 3.0, detector)
    assert [t for t, _ in got][:6] == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    assert [round(x * SECONDS - 0.5) for _, x in got[:6]] == [int(start + t) for t, _ in got[:6]]


@pytest.mark.parametrize("start,beep", [(1.0, 3.0), (8.0, 12.0), (10.5, 12.0)])
def test_audio_readers_put_the_beep_where_it_is(ts, tmp_path, start, beep):
    from clipwatcher.judge import transcribe
    from clipwatcher.render import cold_open

    onset = beep_onset(transcribe.extract_wav(ts, start, 6.0, tmp_path / "w.wav"))
    assert onset is not None and abs(start + onset - beep) <= 0.06  # words: what Whisper is given
    series = cold_open.loudness(ts, start, 6.0)
    loud = next(t for t, v in series if v > -40.0)  # 400 ms loudness window, a value every 100 ms
    assert abs(start + loud - beep) <= 0.25


def test_loudness_pass_measures_the_clip_that_is_rendered(ts):
    from clipwatcher.render.template import _measure, _seek

    silent = _measure(ts, *_seek(7.0), 4.9)  # 7.0-11.9 s: no beep in it
    assert silent is None or float(silent["input_i"]) < -60
    with_beep = _measure(ts, *_seek(10.9), 1.2)  # 10.9-12.1 s
    assert with_beep is not None and float(with_beep["input_i"]) > -40


def test_calibrate_frames_are_the_moments_asked_for(ts, tmp_path):
    """Layout detection reads single frames too. With a plain -ss on a .ts whose keyframes are 4 s apart,
    most of the six frames around a peak came back as one and the same keyframe: no motion to compare."""
    from clipwatcher import calibrate
    from clipwatcher.layout import PEAK_OFFSETS

    times = [9.75 + o for o in PEAK_OFFSETS]  # 8.25, 8.75, ... 10.75 s
    peak = calibrate.frames_at(ts, tmp_path / "peak", times, height=None)
    assert [t for t, _ in peak] == times
    assert [second_shown(p) for _, p in peak] == [int(t) for t in times]
    small = calibrate.frames_at(ts, tmp_path / "small", times[:2], height=90)
    with Image.open(small[0][1]) as im:
        assert im.size == (160, 90)
    spread = calibrate.sample_frames(ts, tmp_path / "all", 6)  # about 2.5, 5.5, ... 17.5 s of the 20 s
    assert len(spread) == 6
    assert [second_shown(p) for _, p in spread] == [int(t) for t, _ in spread]
    part = calibrate.sample_frames(ts, tmp_path / "part", 4, window=(10.0, 14.0))  # only that stretch
    assert [t for t, _ in part] == [10.5, 11.5, 12.5, 13.5]
    assert [second_shown(p) for _, p in part] == [10, 11, 12, 13]
    off = calibrate.sample_frames(ts, tmp_path / "off", 4, window=(500.0, 510.0))  # past the end: whole file
    assert [round(t) for t, _ in off] == [round(t) for t, _ in calibrate.sample_frames(ts, tmp_path / "w", 4)]
