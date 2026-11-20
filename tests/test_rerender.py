"""A staged clip keeps what its Short was rendered with, and the companion app renders it again from that."""

import json
import subprocess

import pytest

from clipwatcher import config, events
from clipwatcher.models import Word
from clipwatcher.render import recipe
from clipwatcher.ui import jobs
from clipwatcher.ui.runs import command


def _staged(root, cid="zack-live-1", **meta):
    d = root / cid
    d.mkdir(parents=True)
    base = {"streamer": "zack", "title": "A title", "hook": "A hook", "state": "READY", "captions": "word"}
    (d / "meta.json").write_text(json.dumps({**base, **meta}))
    (d / "short_9x16.mp4").write_bytes(b"old short")
    (d / "clean_16x9.mp4").write_bytes(b"clean")
    return d


@pytest.fixture
def fake_render(monkeypatch, tmp_path):
    """The renderer and ffprobe are stood in for: what counts here is what they are asked and what is kept."""
    calls = []

    def render_short(src, start_s, dur_s, out, **kw):
        calls.append({"src": src, "start_s": start_s, "dur_s": dur_s, **kw})
        out.write_bytes(b"new short")
        return out

    monkeypatch.setattr("clipwatcher.render.template.render_short", render_short)
    monkeypatch.setattr(jobs, "_duration", lambda media: 38.0)
    monkeypatch.setenv("CLIPWATCHER_EVENTS", str(tmp_path / "events.jsonl"))
    return calls


def _events(tmp_path):
    return [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]


def test_a_ready_clip_is_rendered_again_from_its_clean_cut_and_recipe(tmp_path, fake_render):
    root = tmp_path / "staging"
    d = _staged(root)
    words = [Word(1.0, 1.4, "LETS"), Word(1.4, 1.9, "GO")]
    layout = {"has_webcam": True, "cam": (0, 0, 480, 270), "game": None, "face": None, "reframe": False}
    recipe.keep(d, words=words, hook="A hook", layout=layout, beats=[(2.0, "hype", "*W*")])
    out = jobs.rerender("zack-live-1", captions="pop", layout="stack", root=root)
    (call,) = fake_render
    assert call["src"] == d / "clean_16x9.mp4" and (call["start_s"], call["dur_s"]) == (0.0, 38.0)
    assert call["captions"] == "pop" and call["layout"] == "stack" and call["words"] == words
    assert (
        call["cam"] == (0, 0, 480, 270)
        and call["beats"] == [(2.0, "hype", "*W*")]
        and call["hook"] == "A hook"
    )
    assert out.read_bytes() == b"new short" and not (d / "short_9x16.new.mp4").exists()
    meta = json.loads((d / "meta.json").read_text())
    assert meta["captions"] == "pop" and meta["render_layout"] == "stack" and meta["state"] == "READY"
    kinds = [(e["kind"], e.get("step"), e.get("state")) for e in _events(tmp_path)]
    assert kinds == [
        ("step", "render_short", "start"),
        ("step", "render_short", "done"),
        ("step", "stage", "start"),
        ("step", "stage", "done"),
        ("staged", None, None),
    ]
    assert _events(tmp_path)[-1]["again"] is True and _events(tmp_path)[-1]["clip_id"] == "zack-live-1"


def test_a_clip_staged_before_recipes_is_worked_out_from_its_video(tmp_path, fake_render, monkeypatch):
    root = tmp_path / "staging"
    d = _staged(root)

    class Lay:
        def as_render_kwargs(self):
            return {"has_webcam": False, "cam": None, "game": None, "face": None, "reframe": False}

    monkeypatch.setattr("clipwatcher.judge.transcribe.transcribe", lambda media, start, dur, lang: [])
    monkeypatch.setattr("clipwatcher.layout.resolve", lambda media, streamer, **kw: Lay())
    jobs.rerender("zack-live-1", root=root)
    (call,) = fake_render
    assert call["captions"] == "word" and call["layout"] == "split" and call["has_webcam"] is False
    assert [e["step"] for e in _events(tmp_path) if e["kind"] == "step" and e["state"] == "done"] == [
        "transcribe",
        "layout",
        "render_short",
        "stage",
    ]
    assert json.loads((d / "meta.json").read_text())["captions"] == ""  # no words: no captions on it


def test_a_decided_clip_keeps_its_video(tmp_path, fake_render):
    root = tmp_path / "staging"
    d = _staged(root, state="APPROVED")
    with pytest.raises(ValueError, match="already approved"):
        jobs.rerender("zack-live-1", captions="pop", root=root)
    assert (d / "short_9x16.mp4").read_bytes() == b"old short" and fake_render == []
    for bad in ({"captions": "comic"}, {"layout": "wide"}):
        with pytest.raises(ValueError):
            jobs.rerender("zack-live-1", root=root, **bad)
    with pytest.raises(ValueError, match="No such clip"):
        jobs.rerender("nobody-live-9", root=root)


def test_a_render_that_fails_leaves_the_old_short(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    d = _staged(root)
    recipe.keep(d, words=[], hook=None, layout={"has_webcam": False})

    def broken(src, start_s, dur_s, out, **kw):
        out.write_bytes(b"half a video")
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr("clipwatcher.render.template.render_short", broken)
    monkeypatch.setattr(jobs, "_duration", lambda media: 38.0)
    with pytest.raises(subprocess.CalledProcessError):
        jobs.rerender("zack-live-1", captions="pop", root=root)
    assert (d / "short_9x16.mp4").read_bytes() == b"old short" and not (d / "short_9x16.new.mp4").exists()
    assert json.loads((d / "meta.json").read_text())["captions"] == "word"


def test_the_app_builds_the_render_command_from_checked_values():
    cmd = command("render", {"clip": "zack-live-1", "captions": "pop", "layout": "stack"})
    assert cmd[1:] == [
        "-m",
        "clipwatcher.ui.jobs",
        "render",
        "zack-live-1",
        "--captions",
        "pop",
        "--layout",
        "stack",
    ]
    assert command("render", {"clip": "zack-live-1"})[-2:] == ["render", "zack-live-1"]
    for bad in (
        {"clip": "../etc"},
        {"clip": "zack-live-1", "captions": "comic"},
        {"clip": "a", "layout": "x; rm"},
        {},
    ):
        with pytest.raises(ValueError):
            command("render", bad)


@pytest.mark.ffmpeg
def test_a_real_short_is_rendered_again_with_another_caption_style(tmp_path, synth_src, monkeypatch):
    """The whole job on real video: a clean cut made by the renderer, then its Short twice, two styles."""
    from clipwatcher.render.template import probe, render_clean

    monkeypatch.setenv("CLIPWATCHER_EVENTS", str(tmp_path / "events.jsonl"))
    root = tmp_path / "staging"
    d = root / "zack-live-1"
    d.mkdir(parents=True)
    render_clean(synth_src, 2.0, 4.0, d / "clean_16x9.mp4")
    (d / "short_9x16.mp4").write_bytes(b"old short")
    (d / "meta.json").write_text(
        json.dumps({"streamer": "zack", "title": "T", "hook": "H", "state": "READY"})
    )
    words = [Word(0.4, 0.9, "NO"), Word(0.9, 1.5, "WAY"), Word(1.6, 2.2, "CHAT"), Word(2.3, 3.0, "LOOK")]
    recipe.keep(
        d, words=words, hook="H", layout={"has_webcam": False, "reframe": False}, beats=[], actions=[]
    )
    sizes = {}
    for style in ("pop", "word"):
        out = jobs.rerender("zack-live-1", captions=style, root=root)
        info = probe(out)
        assert (info.width, info.height) == (1080, 1920) and abs(jobs._duration(out) - 4.0) < 0.35
        sizes[style] = out.stat().st_size
        assert json.loads((d / "meta.json").read_text())["captions"] == style
    assert sizes["pop"] != sizes["word"]  # the captions are burned in: another style, another picture
    assert config.dry_run() and events.path() is not None
