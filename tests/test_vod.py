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
    _fake(
        monkeypatch, "clipwatcher.judge.frames", keyframes_lowres=lambda *a, **k: [], frame_count=lambda d: 8
    )
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
    # --out: the default is ./staging, the real queue (this test used to create staging/clips.db in the repo)
    args = ["--vod", "555555", "--streamer", "foo", "--chat", str(chat), "--video", str(tmp_path / "v.mp4")]
    vod.main(
        [*args, "--no-judge", "--top", "2", "--work", str(tmp_path / "w"), "--out", str(tmp_path / "st")]
    )
    assert len(staged) == 2 and staged[0]["judge_source"] == "none"


def test_judge_rejected_clip_is_not_staged(tmp_path, monkeypatch, capsys):
    """The VOD lane staged everything as READY, also what the judge marked not publishable or flagged as a
    spoiler / music risk, with the judge score as queue score: auto-approve could have published it."""
    chat = synth.to_tdl_json(synth.make_chat(), tmp_path / "chat.json")
    monkeypatch.setattr(vod, "run_cmd", lambda cmd: None)
    verdicts = [
        JudgeResult(
            9, 9, "", 0, 20, "h", ["t"], "s", ok=False, score=9, flags={"mu": True}, reject_reason="music"
        ),
        JudgeResult(7, 7, "", 0, 20, "h", ["t"], "s", ok=True, score=7),
        JudgeResult(3, 3, "", 0, 20, "h", ["t"], "s", ok=False, score=3, reject_reason="dead air"),
    ]
    _fake(monkeypatch, "clipwatcher.judge.transcribe", transcribe=lambda *a, **k: [])

    def no_frames(*a, **k):
        raise OSError("ffmpeg could not read the window")  # must not abort the run either

    _fake(monkeypatch, "clipwatcher.judge.frames", keyframes_lowres=no_frames, frame_count=lambda d: 8)
    _fake(monkeypatch, "clipwatcher.judge.claude", judge_clip=lambda *a, **k: verdicts.pop(0))
    rendered, staged = [], []
    _fake(
        monkeypatch, "clipwatcher.render.template",
        render_short=lambda s, a, d, out, **k: rendered.append(out) or out,
        render_clean=lambda s, a, d, out, **k: out,
    )  # fmt: skip
    _fake(
        monkeypatch,
        "clipwatcher.queue.staging",
        stage_package=lambda c, s, cl, m, **k: staged.append(m) or tmp_path,
    )
    rc = vod.main(["--vod", "777777", "--streamer", "foo", "--chat", str(chat), "--top", "3",
                   "--work", str(tmp_path / "w"), "--out", str(tmp_path / "st")])  # fmt: skip
    assert rc == 0 and len(staged) == 1 and len(rendered) == 1  # rejected clips are not even rendered
    assert staged[0]["judge_score"] == 7 and capsys.readouterr().out.count("rejected by the judge") == 2


def test_chat_lines_keep_the_reaction_at_the_end_of_the_window():
    from clipwatcher.models import ChatMessage

    msgs = [ChatMessage(t * 10, f"u{t}", f"line {t}") for t in range(5000)]  # 100 messages a second
    lines = vod._chat_lines(msgs, 0, 50)
    assert len(lines) == 2000 and lines[-1] == "u4999: line 4999"  # was: the first 60, all before the moment


def test_viewer_clips_confirm_and_reorder_vod_candidates():
    from clipwatcher.detect.viewer_clips import vod_clip_moments
    from clipwatcher.models import Candidate

    def post(payload):
        assert payload["variables"] == {"login": "foo"}
        nodes = [
            {"videoOffsetSeconds": 1000, "durationSeconds": 30, "video": {"id": "42"}},
            {"videoOffsetSeconds": 50, "durationSeconds": 30, "video": {"id": "another vod"}},
            {"videoOffsetSeconds": None, "durationSeconds": 30, "video": {"id": "42"}},
            {"videoOffsetSeconds": 77, "durationSeconds": 20, "video": None},
        ]
        return {"data": {"user": {"clips": {"edges": [{"node": n} for n in nodes]}}}}

    assert vod_clip_moments("Foo", "42", post=post) == [1015.0]
    assert vod_clip_moments("foo", "42", post=lambda p: {"errors": ["nope"]}) == []
    assert vod_clip_moments("foo", "42") == []  # offline (tests never reach Twitch): no clips, no error

    a, b, c = (
        Candidate("foo", p, p - 40, p + 19, sc, sc) for p, sc in ((500, 9.0), (1020, 8.0), (3000, 7.0))
    )
    top = vod.confirmed_top([a, b, c], [1015.0], 2)
    assert (
        [x.peak_s for x in top] == [1020, 500] and top[0].final_score == 9.5 and top[0].signals["viewer_clip"]
    )
    assert vod.confirmed_top([a, b, c], [], 2) == [a, b]
    loud = [(i / 10, -8.0 if 400 <= i < 410 else -30.0 + (i % 5) * 0.5) for i in range(590)]
    both = vod.with_loudness(top[0], loud)  # the loudness bonus is added to the clip bonus, not instead of it
    assert both.final_score == 8.0 + 1.5 + 1.5 and both.signals["viewer_clip"] and both.signals["loud_z"] > 5
