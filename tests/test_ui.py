"""The companion app's server side: what it reads, the runs it starts and the guards around both."""

import http.client
import json
import signal
import sys
import threading
import time

import pytest

from clipwatcher import config
from clipwatcher.judge import ledger
from clipwatcher.ui import data, settings
from clipwatcher.ui.runs import Run, RunManager, command
from clipwatcher.ui.server import App, make_server

# a stand-in for a lane: it says a few lines, reports two events and ends
LANE = (
    "import json, os, sys, time\n"
    "print('joining'); sys.stdout.flush()\n"
    "f = open(os.environ['CLIPWATCHER_EVENTS'], 'a')\n"
    "say = lambda **d: (f.write(json.dumps(d) + chr(10)), f.flush())\n"
    "say(t=1.0, kind='recording', streamer='zack', ring='/ring/zack')\n"
    "time.sleep(0.3)\n"
    "say(t=2.0, kind='staged', clip_id='zack-live-1', title='T')\n"
    "print('done')\n"
)


def _wait(run, timeout=20.0):
    end = time.time() + timeout
    while run.state == "running" and time.time() < end:
        time.sleep(0.05)
    return run


def _staged(root, cid, **meta):
    d = root / cid
    d.mkdir(parents=True)
    base = {
        "streamer": "zack",
        "credit": "twitch.tv/zack",
        "title": "A title",
        "hook": "A hook",
        "state": "READY",
    }
    (d / "meta.json").write_text(json.dumps({**base, **meta}))
    (d / "short_9x16.mp4").write_bytes(bytes(range(256)) * 40)
    return d


def test_clips_are_read_from_the_staging_folder(tmp_path):
    _staged(
        tmp_path,
        "zack-live-1",
        chat_score=3.2,
        final_score=4.5,
        start_s=10.0,
        end_s=48.0,
        judge_source="heuristic",
    )
    _staged(
        tmp_path,
        "n3on-live-2",
        credit="kick.com/n3on",
        streamer="n3on",
        seconds=32,
        timings={"cut": 0.3, "render_short": 14.0},
    )
    (tmp_path / "approved").mkdir()  # not a clip: no meta.json
    got = {c["id"]: c for c in data.clips(tmp_path)}
    assert set(got) == {"zack-live-1", "n3on-live-2"}
    assert got["zack-live-1"]["seconds"] == 38.0 and got["zack-live-1"]["platform"] == "twitch"
    assert got["n3on-live-2"]["platform"] == "kick" and got["n3on-live-2"]["files"] == {
        "short_9x16.mp4": 10240
    }
    assert (
        data.clip("n3on-live-2", tmp_path)["timings"]["cut"] == 0.3 and data.clip("../etc", tmp_path) is None
    )
    # only the two videos of a clip folder are ever handed out
    assert data.media_path("zack-live-1", "short_9x16.mp4", tmp_path).is_file()
    assert data.media_path("zack-live-1", "meta.json", tmp_path) is None
    assert data.media_path("..", "short_9x16.mp4", tmp_path) is None


def test_insights_add_up_timings_and_the_ledger(tmp_path, monkeypatch):
    _staged(
        tmp_path,
        "a",
        timings={"cut": 0.2, "render_short": 10.0, "wait": 5.0},
        seconds=30,
        signals={"zU": 2.0, "zE": 6.0},
    )
    _staged(tmp_path, "b", timings={"cut": 0.4, "render_short": 14.0}, seconds=40, judge_source="claude")
    prices = {
        "judge": {"prices": {"big": {"input": 2.0, "output": 10.0, "cache_read": 0.1, "cache_write": 2.5}}}
    }
    monkeypatch.setattr(config, "global_config", lambda: prices)
    ledger.record(
        task="judge",
        model="big",
        usage={"input_tokens": 1000, "output_tokens": 100},
        streamer="zack",
        ok=True,
        score=7,
    )
    ledger.record(
        task="judge",
        model="big",
        usage={"input_tokens": 1000, "output_tokens": 100},
        streamer="zack",
        ok=False,
        score=3,
    )
    ledger.record(task="judge", model="big", error="timeout")
    ins = data.insights(
        tmp_path, runs=[{"kind": "tests", "started": 1.0, "tests": {"passed": 286, "seconds": 98.0}}]
    )
    steps = {s["step"]: s for s in ins["speed"]["steps"]}
    assert (
        steps["render_short"]["avg_s"] == 12.0
        and steps["cut"]["n"] == 2
        and ins["speed"]["tests"][0]["passed"] == 286
    )
    assert [p["work_s"] for p in ins["speed"]["per_clip"]] == [10.2, 14.4] or ins["speed"][
        "avg_work_s"
    ] == 12.3
    assert ins["cost"]["usd"] == 0.006 and ins["cost"]["calls"] == 2 and ins["cost"]["failed"] == 1
    assert ins["cost"]["by_kind"] == {"input": 0.004, "output": 0.002, "cache_read": 0.0, "cache_write": 0.0}
    assert (ins["judge"]["accepted"], ins["judge"]["rejected"], ins["judge"]["errors"]) == (1, 1, 1)
    assert ins["judge"]["sources"] == {"none": 1, "claude": 1} and ins["detector"]["signals"]["zE"] == 6.0


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setenv("CLIPWATCHER_LOCAL", "1")
    (tmp_path / "global.yaml").write_text(
        "detector:\n  keep_q: 0.99\n  max_per_hour: 8\nrender:\n  captions: word\n  jump_cuts: true\n"
    )
    config.global_config.cache_clear()
    yield tmp_path
    config.global_config.cache_clear()


def test_settings_are_saved_beside_the_global_file_and_checked_first(cfgdir):
    kept = settings.save({"detector.keep_q": 0.985, "render.captions": "pop", "detector.max_per_hour": 8})
    assert kept == {"detector.keep_q": 0.985, "render.captions": "pop"}  # 8 is what global.yaml says already
    assert config.global_config()["detector"] == {"keep_q": 0.985, "max_per_hour": 8}
    fields = {f["key"]: f for g in settings.schema() for f in g["fields"]}
    assert fields["detector.keep_q"]["value"] == 0.985 and fields["detector.keep_q"]["default"] == 0.99
    for bad in (
        {"detector.keep_q": 2},
        {"render.captions": "comic"},
        {"render.jump_cuts": "yes"},
        {"secrets.token": "x"},
    ):
        with pytest.raises(ValueError):
            settings.save(bad)
    assert config.global_config()["render"]["captions"] == "pop"  # a refused save changes nothing
    # one field alone (the caption choice of the Run page) leaves the others as they were
    assert settings.save({"render.captions": "bounce"}, merge=True) == {
        "detector.keep_q": 0.985,
        "render.captions": "bounce",
    }
    settings.save({})
    assert not (cfgdir / "local.yaml").exists()


def test_the_recording_quality_is_a_choice_in_words(cfgdir):
    """720p or the sharpest the stream offers (the webcam of a Short is cut out of the recording)."""
    f = {f["key"]: f for g in settings.schema() for f in g["fields"]}["buffer.quality"]
    assert f["options"] == [settings.QUALITY_LIGHT, "best"] and set(f["labels"]) == set(f["options"])
    assert settings.save({"buffer.quality": "best"}) == {"buffer.quality": "best"}
    assert config.global_config()["buffer"]["quality"] == "best"  # what the live lane records with
    with pytest.raises(ValueError):
        settings.save({"buffer.quality": "4k; rm -rf"})
    # a ladder someone wrote into the config by hand stays a choice, and saving the page does not refuse it
    (cfgdir / "local.yaml").write_text("buffer:\n  quality: 480p,best\n")
    config.global_config.cache_clear()
    f = {f["key"]: f for g in settings.schema() for f in g["fields"]}["buffer.quality"]
    assert f["options"][-1] == "480p,best" and f["value"] == "480p,best"
    assert settings.save({"buffer.quality": "480p,best"}) == {"buffer.quality": "480p,best"}


def test_a_run_gathers_what_the_command_says_and_what_the_lane_reports(tmp_path):
    m = RunManager(tmp_path, build=lambda kind, params: [sys.executable, "-c", LANE])
    run = _wait(m.start("live", {"streamer": "zack"}))
    kinds = [e["kind"] for e in run.events]
    assert run.state == "done" and kinds[0] == "start" and kinds[-1] == "end"
    assert {"log", "recording", "staged"} <= set(kinds) and [e["seq"] for e in run.events] == list(
        range(1, len(kinds) + 1)
    )
    assert run.ring == "/ring/zack" and run.staged == ["zack-live-1"]
    saved = json.loads((run.dir / "run.json").read_text())
    assert (
        saved["state"] == "done"
        and saved["staged"] == ["zack-live-1"]
        and "joining" in (run.dir / "log.txt").read_text()
    )
    later = RunManager(tmp_path)  # another session: the run is still there, and can be watched again
    assert m.history()[0]["id"] == run.id and later.history()[0]["state"] == "done"
    assert m.history()[0]["replayable"] and later.history()[0]["replayable"]
    (run.dir / "replay.jsonl").unlink()  # a run of before the events were kept
    assert RunManager(tmp_path).history()[0]["replayable"] is False
    (run.dir / "replay.jsonl").write_text("".join(json.dumps(e) + chr(10) for e in run.events))
    assert later.events_of(run.id) == m.events_of(run.id) == run.events and later.events_of("../x") is None


def test_one_run_at_a_time_and_a_run_can_be_stopped(tmp_path):
    m = RunManager(tmp_path, build=lambda kind, params: [sys.executable, "-c", "import time; time.sleep(60)"])
    run = m.start("live", {"streamer": "zack"})
    with pytest.raises(RuntimeError):
        m.start("tests", {})
    assert m.stop(run.id) and _wait(run).state == "stopped" and not m.stop(run.id)


def test_stop_works_when_the_app_was_started_in_the_background(tmp_path):
    """A shell starts background jobs with SIGINT ignored, and children inherit that."""
    was = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        m = RunManager(
            tmp_path, build=lambda kind, params: [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        run = m.start("live", {"streamer": "zack"})
        t0 = time.time()
        assert m.stop(run.id) and _wait(run).state == "stopped" and time.time() - t0 < 15  # pressed at once
    finally:
        signal.signal(signal.SIGINT, was)


def test_a_test_run_does_not_see_this_machines_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPWATCHER_STAGING", "/the/real/staging")
    monkeypatch.setenv("DRY_RUN", "0")
    show = (
        "import os; e = os.environ; "
        "print(e.get('CLIPWATCHER_STAGING'), e.get('DRY_RUN'), 'CLIPWATCHER_EVENTS' in e)"
    )
    m = RunManager(tmp_path, build=lambda kind, params: [sys.executable, "-c", show])
    said = [e["line"] for e in _wait(m.start("tests", {})).events if e["kind"] == "log"]
    assert said == ["None 1 False"]
    said = [e["line"] for e in _wait(m.start("live", {"streamer": "zack"})).events if e["kind"] == "log"]
    assert said == ["/the/real/staging 0 True"]
    again = _wait(m.start("live", {"streamer": "zack"}))  # the same second: still a run of its own
    assert len({r["id"] for r in m.history()}) == 3 and again.dir.is_dir()


def test_pytest_output_becomes_test_events(tmp_path):
    m, run = RunManager(tmp_path), Run("x", "tests", {}, [], tmp_path)
    m._test_line(run, "tests/test_kick.py::test_ws_chat PASSED                                   [ 42%]")
    m._test_line(run, "tests/test_vod.py::test_x FAILED                                          [ 43%]")
    m._test_line(run, "============ 1 failed, 285 passed, 2 skipped in 98.01s (0:01:38) ============")
    seen = [(e["kind"], e.get("outcome"), e.get("percent")) for e in run.events[:2]]
    assert seen == [("test", "PASSED", 42), ("test", "FAILED", 43)] and run.events[2]["kind"] == "tests_done"
    assert run.tests == {"seen": 2, "failed": 1, "passed": 285, "skipped": 2, "seconds": 98.01}


def test_commands_are_built_from_checked_values(monkeypatch):
    monkeypatch.setattr(config, "streamers", lambda: {"zack": {"platform": "twitch"}})
    assert command("live", {"streamer": "Zack", "minutes": 5})[-4:] == [
        "--streamers",
        "zack",
        "--minutes",
        "5",
    ]
    assert command("live", {"streamer": "anyone_else"})[-4:-2] == [
        "--streamers",
        "anyone_else",
    ]  # like the CLI
    assert command("tests", {"quick": True})[-2:] == ["-m", "not ffmpeg and not remotion"]
    assert "--vod" in command(
        "vod", {"streamer": "zack", "vod": "https://www.twitch.tv/videos/123", "top": 2}
    )
    bad = [
        ("live", {"streamer": ""}),
        ("live", {"streamer": "zack", "minutes": 99999}),
        ("live", {"streamer": "zack; rm -rf"}),
        ("vod", {"streamer": "zack", "vod": "a b"}),
        ("vod", {"streamer": "zack", "vod": "123", "top": 99}),
        ("shell", {"streamer": "zack"}),
    ]
    for kind, params in bad:
        with pytest.raises(ValueError):
            command(kind, params)


def test_the_example_file_is_not_offered_as_a_list_of_channels(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "global_config", lambda: {})
    (tmp_path / "streamers.example.yaml").write_text("streamers:\n  examplestreamer: {platform: twitch}\n")
    assert config.streamers() == {"examplestreamer": {"platform": "twitch"}} and data.own_streamers() == {}
    assert data.overview()["streamers"] == []
    (tmp_path / "streamers.yaml").write_text("streamers:\n  zack: {platform: kick}\n")
    assert [(s["login"], s["platform"]) for s in data.overview()["streamers"]] == [("zack", "kick")]
    monkeypatch.setenv("CLIPWATCHER_TDL", str(tmp_path / "no-such-tool"))
    assert data.overview()["tools"]["twitch_vod"] is False
    monkeypatch.setenv("CLIPWATCHER_TDL", sys.executable)
    assert data.overview()["tools"]["twitch_vod"] is True
    monkeypatch.setattr(data.importlib.util, "find_spec", lambda name: None)
    assert data.overview()["tools"]["face_detector"] is False  # neither mediapipe nor OpenCV
    monkeypatch.setattr(data.importlib.util, "find_spec", lambda name: object() if name == "cv2" else None)
    assert data.overview()["tools"]["face_detector"] is True


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPWATCHER_STAGING", str(tmp_path / "staging"))
    _staged(tmp_path / "staging", "zack-live-1", final_score=4.5)
    app = App(RunManager(tmp_path / "runs", build=lambda kind, params: [sys.executable, "-c", LANE]))
    server, app = make_server("127.0.0.1", 0, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], app
    server.shutdown()
    server.server_close()


def _call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers or {})
    resp = conn.getresponse()
    return resp.status, resp.getheaders(), resp.read()


def test_the_page_its_data_and_a_clip_video_are_served(served):
    port, app = served
    status, _, page = _call(port, "GET", "/")
    assert status == 200 and app.token.encode() in page and b"__TOKEN__" not in page
    status, _, body = _call(port, "GET", "/api/clips")
    assert status == 200 and json.loads(body)[0]["id"] == "zack-live-1"
    assert json.loads(_call(port, "GET", "/api/overview")[2])["dry_run"] is True
    status, headers, part = _call(
        port, "GET", "/media/zack-live-1/short_9x16.mp4", headers={"Range": "bytes=10-19"}
    )
    assert (
        status == 206
        and part == bytes(range(10, 20))
        and dict(headers)["Content-Range"] == "bytes 10-19/10240"
    )
    assert (
        _call(port, "GET", "/media/zack-live-1/meta.json")[0] == 404
        and _call(port, "GET", "/static/../server.py")[0] == 404
    )


def test_only_this_machine_and_only_the_page_may_start_things(served):
    port, app = served
    assert (
        _call(port, "GET", "/api/clips", headers={"Host": "evil.example"})[0] == 403
    )  # a name pointed at 127.0.0.1
    assert _call(port, "POST", "/api/runs", {"kind": "tests"})[0] == 403  # no token: another site's form
    ok = {"X-Companion-Token": app.token, "Content-Type": "application/json"}
    assert (
        _call(port, "POST", "/api/runs", {"kind": "tests"}, {**ok, "Origin": "https://evil.example"})[0]
        == 403
    )
    status, _, body = _call(port, "POST", "/api/runs", {"kind": "live", "params": {"streamer": "zack"}}, ok)
    run = json.loads(body)
    assert status == 201 and run["state"] == "running"
    assert _call(port, "POST", "/api/runs", {"kind": "tests"}, ok)[0] == 409  # one at a time
    # the stream: everything the run did, in order, ending with how it went
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("GET", f"/api/runs/{run['id']}/events?after=0")
    text = conn.getresponse().read().decode()
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["kind"] == "start" and events[-1]["kind"] == "end" and events[-1]["state"] == "done"
    assert "staged" in [e["kind"] for e in events]
    assert json.loads(_call(port, "GET", "/api/runs")[2])[0]["staged"] == ["zack-live-1"]
    again = json.loads(_call(port, "GET", f"/api/runs/{run['id']}")[2])  # the run with all it did
    assert again["events"] == events and again["state"] == "done"
    assert _call(port, "GET", "/api/runs/nope")[0] == 404
