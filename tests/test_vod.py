import sys
import types
from pathlib import Path

from clipwatcher import vod
from clipwatcher.models import JudgeResult, Word
from tests.fixtures import synth


def test_build_videodownload_cmd():
    cmd = vod.build_videodownload_cmd(
        "tdl", "https://www.twitch.tv/videos/123456789", 94.5, 140, Path("o.mp4")
    )
    assert cmd == [
        "tdl", "videodownload", "--id", "123456789", "-b", "94s", "-e", "140s",
        "-q", "1080p60", "-o", "o.mp4",
    ]  # fmt: skip


def test_window_with_pad():
    from clipwatcher.models import Candidate

    c = Candidate("s", 100, 80, 110, 5, 5)
    assert vod.window_with_pad(c, 6) == (74, 116, 6)
    c0 = Candidate("s", 10, 0, 20, 5, 5)
    assert vod.window_with_pad(c0, 6) == (0.0, 26, 0.0)


def _fake(monkeypatch, name, **fns):
    mod = types.ModuleType(name)
    for k, v in fns.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)


def test_main_with_fixture_chat(tmp_path, monkeypatch, capsys):
    chat = synth.to_tdl_json(synth.make_chat(), tmp_path / "chat.json")
    calls = {"dl": [], "short": [], "staged": []}
    monkeypatch.setattr(vod, "run_cmd", lambda cmd: calls["dl"].append(cmd))
    jr = JudgeResult(7, 6, "ctx", 2.0, 20.0, "NO WAY", ["t1", "t2"], "setup")
    _fake(monkeypatch, "clipwatcher.judge.transcribe", transcribe=lambda *a, **k: [Word(0, 1, "hi")])
    _fake(monkeypatch, "clipwatcher.judge.frames", keyframes_lowres=lambda *a, **k: [])
    _fake(monkeypatch, "clipwatcher.judge.claude", judge_clip=lambda *a, **k: jr)

    def short(src, s, d, out, **k):
        calls["short"].append((s, d, k["clip_id"], k["hook"]))
        return out

    _fake(
        monkeypatch,
        "clipwatcher.render.template",
        render_short=short,
        render_clean=lambda s, a, d, out, **k: out,
    )

    def stage(clip_id, s, c, meta, **k):
        calls["staged"].append((clip_id, meta))
        return tmp_path / clip_id

    _fake(monkeypatch, "clipwatcher.queue.staging", stage_package=stage)
    rc = vod.main(
        ["--vod", "https://www.twitch.tv/videos/987654321", "--streamer", "Foo", "--chat", str(chat),
         "--top", "3", "--work", str(tmp_path / "work"), "--out", str(tmp_path / "st")]
    )  # fmt: skip
    assert rc == 0
    assert len(calls["staged"]) == 3 and len(calls["dl"]) == 3
    cid, meta = calls["staged"][0]
    assert cid.startswith("foo-987654321-") or cid.startswith("Foo-") or "987654321" in cid
    assert meta["state"] == "READY" and meta["credit"] == "twitch.tv/foo" and meta["hook"] == "NO WAY"
    assert calls["dl"][0][1] == "videodownload"
    assert "chat" in capsys.readouterr().out


def test_main_no_judge_with_video(tmp_path, monkeypatch):
    chat = synth.to_tdl_json(synth.make_chat(), tmp_path / "chat.json")
    monkeypatch.setattr(vod, "run_cmd", lambda cmd: (_ for _ in ()).throw(AssertionError("no download")))
    staged = []
    _fake(monkeypatch, "clipwatcher.render.template", render_short=lambda s, a, d, out, **k: out,
          render_clean=lambda s, a, d, out, **k: out)  # fmt: skip
    _fake(
        monkeypatch,
        "clipwatcher.queue.staging",
        stage_package=lambda c, s, cl, m, **k: staged.append(m) or tmp_path,
    )
    vod.main(["--vod", "555555", "--streamer", "foo", "--chat", str(chat), "--video", str(tmp_path / "v.mp4"),
              "--no-judge", "--top", "2", "--work", str(tmp_path / "w")])  # fmt: skip
    assert len(staged) == 2 and staged[0]["judge_source"] == "none"
