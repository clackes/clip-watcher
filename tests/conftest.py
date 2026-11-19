"""Shared fixtures: a synthetic MPEG-TS source where every second has its own colour in a top-left box."""

from __future__ import annotations

import subprocess

import pytest

from clipwatcher.config import ffmpeg_bin

SRC_SECONDS = 20


def second_colour(k: int) -> tuple[int, int, int]:
    """RGB of the marker box during second k (distinct per second)."""
    return (k * 12, 255 - k * 12, 128)


def _build(path) -> None:
    boxes = []
    for k in range(SRC_SECONDS):
        r, g, b = second_colour(k)
        boxes.append(
            f"drawbox=x=0:y=0:w=400:h=300:color=0x{r:02x}{g:02x}{b:02x}:t=fill"
            f":enable='between(t,{k},{k + 1})'"
        )
    vf = ",".join(
        [*boxes, "drawbox=x=40:y=700:w=560:h=250:color=0xff00ff:t=fill"]  # "webcam" marker
    )
    cmd = [
        ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=s=1920x1080:r=60:d={SRC_SECONDS}",
        "-f", "lavfi", "-i", f"sine=f=440:r=48000:d={SRC_SECONDS}",
        "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", "240", "-keyint_min", "240", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "128k", "-f", "mpegts", str(path),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)


@pytest.fixture(scope="session")
def synth_src(tmp_path_factory):
    p = tmp_path_factory.mktemp("synth") / "source.ts"
    _build(p)
    return p


@pytest.fixture(autouse=True)
def _no_face_detection_in_pipeline_tests(request, monkeypatch):
    """Pipeline tests use fake media: skip per-clip layout detection (tested in test_layout.py)."""
    if request.module.__name__.endswith("test_layout"):
        return
    from clipwatcher import layout

    monkeypatch.setattr(layout, "resolve", lambda *a, **k: layout.Layout(False, None, None, "test"))


@pytest.fixture(autouse=True)
def _no_category_lookup(monkeypatch):
    """Tests never run streamlink: the live category is unknown (allowed) unless a test sets it."""
    from clipwatcher.ingest import category

    monkeypatch.setattr(category, "stream_category", lambda *a, **k: None)
