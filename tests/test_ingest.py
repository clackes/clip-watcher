import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from clipwatcher import config
from clipwatcher.ingest import disk
from clipwatcher.ingest.hls_map import PdtMap
from clipwatcher.ingest.recorder import AdLog, Recorder, build_ffmpeg_cmd, build_streamlink_cmd, parse_ad_line
from clipwatcher.ingest.ringbuffer import RingBuffer
from clipwatcher.ingest.watcher import Heartbeat, KickPoller, Supervisor, TwitchPoller


def _fill(rb, n, t0=1_000_000):
    for i in range(n):
        p = rb.root.parent / f"s{i}.ts"
        p.write_bytes(b"x" * 10)
        rb.add_segment(p, t0 + i * 10_000)


def test_prune_window_and_newest(tmp_path):
    rb = RingBuffer(tmp_path / "r", window_min=1, min_free_disk_pct=0)
    _fill(rb, 10)
    res = rb.prune(now_ms=1_000_000 + 10 * 10_000 + 300_000)  # everything is older than window
    assert len(res.deleted) == 9 and rb.segments()[-1]["name"] == "s9.ts"
    assert (rb.root / "s9.ts").exists() and not (rb.root / "s0.ts").exists()


def test_prune_pin(tmp_path):
    rb = RingBuffer(tmp_path / "r", window_min=1, min_free_disk_pct=0)
    _fill(rb, 10)
    rb.pin(1_020_000, 1_050_000, ttl_s=100, now=0)
    res = rb.prune(now_ms=2_000_000, now=1)
    assert {"s2.ts", "s3.ts", "s4.ts"}.isdisjoint(res.deleted)
    assert len(res.deleted) == 6
    rb.prune(now_ms=2_000_000, now=1000)  # pin expired
    assert [r["name"] for r in rb.segments()] == ["s9.ts"]


def test_prune_disk_low_still_prunes(tmp_path, monkeypatch):
    # Regression: pruning used to stop below 15% free, so a Mac that started at 10% free grew every ring
    # until the disk was full. Pruning must always run; low disk only raises the alert.
    rb = RingBuffer(tmp_path / "r", window_min=1, min_free_disk_pct=15, min_free_disk_gb=5)
    _fill(rb, 5)
    monkeypatch.setattr("clipwatcher.ingest.ringbuffer.free_pct", lambda p: 5.0)
    monkeypatch.setattr("clipwatcher.ingest.ringbuffer.free_gb", lambda p: 2.0)
    res = rb.prune(now_ms=9_999_999)
    assert res.alert and res.deleted and len(rb.segments()) == 1


def test_low_pct_but_enough_gb_is_not_low(tmp_path, monkeypatch):
    rb = RingBuffer(tmp_path / "r", min_free_disk_pct=15, min_free_disk_gb=5)
    monkeypatch.setattr("clipwatcher.ingest.ringbuffer.free_pct", lambda p: 9.6)
    monkeypatch.setattr("clipwatcher.ingest.ringbuffer.free_gb", lambda p: 22.0)
    assert rb.low_disk() is None


def test_recorder_pauses_and_resumes_on_low_disk(tmp_path, monkeypatch):
    import asyncio

    from clipwatcher.ingest.recorder import Recorder

    alerts, events = [], []

    async def on_alert(t):
        alerts.append(t)

    rec = Recorder("twitch", "x", "s", tmp_path, on_alert=on_alert)

    async def spawn():
        events.append("spawn")

    async def kill():
        events.append("kill")

    monkeypatch.setattr(rec, "_spawn", spawn)
    monkeypatch.setattr(rec, "_kill_pipeline", kill)
    low = {"v": "low"}
    monkeypatch.setattr(rec.ring, "low_disk", lambda: low["v"])

    async def run():
        await rec.check_once()
        await rec.check_once()  # still low: no second kill
        low["v"] = None
        await rec.check_once()

    asyncio.run(run())
    assert events == ["kill", "spawn"] and not rec.paused and len(alerts) == 2


def test_zero_byte_segment_dropped(tmp_path):
    rb = RingBuffer(tmp_path / "r")
    p = tmp_path / "z.ts"
    p.write_bytes(b"")
    assert rb.add_segment(p, 1) is None and not p.exists()


def test_free_pct(tmp_path):
    assert 0 <= disk.free_pct(tmp_path) <= 100


def test_pdtmap_interp_and_gap():
    m = PdtMap([(0, 1_000_000), (1, 1_010_000), (2, 1_020_000), (3, 1_100_000), (4, 1_110_000)])
    assert m.epoch_ms(5) == pytest.approx(1_005_000)
    assert m.epoch_ms(35) == pytest.approx(1_105_000)
    assert m.gaps() == [(1_030_000, 1_100_000)]
    assert m.offset_s(1_015_000) == pytest.approx(15)
    assert m.offset_s(1_050_000) is None
    assert m.offset_s(1_105_000) == pytest.approx(35)


def test_streamlink_cmd():
    c = build_streamlink_cmd("twitch", "foo")
    assert (
        c[-3:] == ["--stdout", "https://twitch.tv/foo", "720p60,720p,best"][-3:]
        or c[-2] == "https://twitch.tv/foo"
    )
    for f in (
        "--retry-streams",
        "--retry-max",
        "--retry-open",
        "--hls-playlist-reload-attempts",
        "--twitch-supported-codecs",
    ):
        assert f in c
    k = build_streamlink_cmd("kick", "bar", "1080p60,best")
    assert "https://kick.com/bar" in k and "--twitch-supported-codecs" not in k and k[-1] == "1080p60,best"


def test_ffmpeg_cmd(tmp_path):
    c = build_ffmpeg_cmd(tmp_path, "r1", ffmpeg="ffmpeg")
    assert c[c.index("-segment_time") + 1] == "10" and "-reset_timestamps" in c and "csv" in c


def test_ad_parser_and_log(tmp_path):
    assert parse_ad_line("[plugins.twitch][info] Detected advertisement break of 30 seconds") == {
        "kind": "start",
        "duration_s": 30.0,
    }
    assert parse_ad_line("[plugins.twitch][info] Resuming stream output") == {"kind": "end"}
    assert parse_ad_line("random line") is None
    log = AdLog(tmp_path / "ads.jsonl")
    assert log.feed("Detected advertisement break of 30 seconds", 1000) == {"start_ms": 1000, "end_ms": 31000}
    log.feed("advertisement ongoing", 5000)
    assert log.feed("Resuming stream output", 9000) == {"start_ms": 5000, "end_ms": 9000}
    assert len((tmp_path / "ads.jsonl").read_text().splitlines()) == 2


def test_recorder_ingest_run(tmp_path):
    rec = Recorder("twitch", "foo", "123", tmp_path)
    (rec.dir / "seg_a_00000.ts").write_bytes(b"1")
    os.utime(rec.dir / "seg_a_00000.ts", (5_012, 5_012))  # segment finished 2 s after its 10 s of video
    (rec.dir / "seg_a_00001.ts").write_bytes(b"")
    (rec.dir / "a.csv").write_text("seg_a_00000.ts,0.0,10.0\nseg_a_00001.ts,10.0,20.0\n")
    assert rec.ingest_run("a", 5_000_000) == 1
    assert rec.ring.segments()[0]["start_ms"] == 5_000_000 and not (rec.dir / "seg_a_00001.ts").exists()
    assert rec.newest_segment_age_s(now=5_020) is not None


def test_recorder_anchors_run_that_waited_for_the_stream(tmp_path):
    """Regression: streamlink spawned 1 h before the channel went live; segments must not be 1 h early."""
    rec = Recorder("twitch", "foo", "123", tmp_path)
    (rec.dir / "seg_b_00000.ts").write_bytes(b"1")
    (rec.dir / "seg_b_00001.ts").write_bytes(b"1")
    os.utime(rec.dir / "seg_b_00000.ts", (8_610, 8_610))  # live at 8_600 s, spawned at 5_000 s
    (rec.dir / "b.csv").write_text("seg_b_00000.ts,0.0,10.0\n")
    assert rec.ingest_run("b", 5_000_000) == 1
    (rec.dir / "b.csv").write_text("seg_b_00000.ts,0.0,10.0\nseg_b_00001.ts,10.0,20.0\n")
    assert rec.ingest_run("b", 5_000_000) == 1  # later polls keep the same anchor
    assert [r["start_ms"] for r in rec.ring.segments()] == [8_600_000, 8_610_000]


class FakeHttp:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers=None, data=None, timeout=15):
        self.calls.append((method, url))
        if "oauth2/token" in url or "oauth/token" in url:
            return {"access_token": "tok", "expires_in": 3600}
        if "helix/streams" in url:
            return {
                "data": [
                    {
                        "id": "99",
                        "user_login": "Foo",
                        "viewer_count": 42,
                        "title": "t",
                        "game_name": "g",
                        "started_at": "2026-01-01T00:00:00Z",
                        "type": "live",
                    }
                ]
            }
        return {
            "data": [
                {
                    "slug": "bar",
                    "stream_title": "k",
                    "category": {"name": "c"},
                    "stream": {"is_live": True, "viewer_count": 7, "start_time": "T"},
                }
            ]
        }


def test_twitch_poll():
    h = FakeHttp()
    st = TwitchPoller("c", "s", http=h).poll(["foo", "baz"])
    assert st["foo"]["live"] and st["foo"]["stream_id"] == "99" and st["foo"]["viewers"] == 42
    assert st["baz"]["live"] is False
    assert "user_login=foo&user_login=baz" in h.calls[-1][1]


def test_kick_poll():
    st = KickPoller("c", "s", http=FakeHttp()).poll(["bar", "qux"])
    assert st["bar"]["live"] and st["bar"]["game"] == "c" and not st["qux"]["live"]


class FakeRec:
    def __init__(self, platform, login, stream_id, root, **kw):
        self.stream_id, self.started, self.stopped, self.restarts = stream_id, True, False, 0
        self.age = 0

    async def start(self):
        self.restarts += 1

    async def stop(self):
        self.stopped = True

    def newest_segment_age_s(self, now=None):
        return self.age


def test_supervisor(tmp_path):
    h = FakeHttp()
    chats = []

    async def chat(p, lg, sid):
        chats.append(lg)
        await asyncio.sleep(10)

    async def go():
        hb = Heartbeat(tmp_path / "q.db")
        sup = Supervisor(
            {"foo": {}},
            tmp_path,
            hb,
            twitch=TwitchPoller("c", "s", http=h),
            recorder_factory=FakeRec,
            chat_factory=chat,
        )
        await sup.tick()
        rec = sup.recorders["foo"]
        assert rec.restarts == 1 and hb.last("watcher")
        rec.age = 500
        await sup.tick()
        assert rec.restarts == 2  # stale segments -> restart
        await sup.tick({"foo": {"live": False}})
        assert rec.stopped and "foo" not in sup.recorders
        sup.chat_tasks.clear()

    asyncio.run(go())
    assert chats == ["foo"]


@pytest.mark.ffmpeg
def test_cut(tmp_path):
    rb = RingBuffer(tmp_path / "r", min_free_disk_pct=0)
    for i in range(3):
        p = tmp_path / f"c{i}.ts"
        subprocess.run(
            [
                config.ffmpeg_bin(),
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=s=160x120:r=25:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=d=1",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-shortest",
                str(p),
            ],
            check=True,
        )
        rb.add_segment(p, 1_000_000 + i * 1000, 1.0)
    out, local = rb.cut(1_001_500, 1_002_500, tmp_path / "o" / "cut.ts")
    assert out.exists() and local == pytest.approx(0.5)
    dur = float(
        subprocess.run(
            [
                config.ffprobe_bin(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(out),
            ],
            capture_output=True,
            text=True,
        ).stdout
    )
    assert dur == pytest.approx(2.0, abs=0.3)
    with pytest.raises(LookupError):
        rb.cut(5, 6, tmp_path / "x.ts")


def test_covered_until_ms(tmp_path):
    from clipwatcher.ingest.ringbuffer import RingBuffer

    rb = RingBuffer(tmp_path / "ring", window_min=30, segment_s=10)
    assert rb.covered_until_ms() == 0
    for i in range(3):
        seg = tmp_path / f"s{i}.ts"
        seg.write_bytes(b"x" * 10)
        rb.add_segment(seg, 1_000_000 + i * 10_000, duration_s=10)
    assert rb.covered_until_ms() == 1_030_000


def test_streamlink_bin_found_without_activated_venv(monkeypatch):
    # Regression: a bare "streamlink" made every recorder exit at once when the venv wasn't on PATH.
    from clipwatcher import config

    config.streamlink_bin.cache_clear()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("CLIPWATCHER_STREAMLINK", raising=False)
    try:
        assert Path(config.streamlink_bin()).stem == "streamlink"
    finally:
        config.streamlink_bin.cache_clear()


def test_recorder_respawns_dead_pipeline(tmp_path, monkeypatch):
    import asyncio
    import types

    from clipwatcher.ingest.recorder import Recorder

    rec = Recorder("twitch", "x", "s", tmp_path)
    events = []

    async def spawn():
        events.append("spawn")

    async def kill():
        events.append("kill")

    monkeypatch.setattr(rec, "_spawn", spawn)
    monkeypatch.setattr(rec, "_kill_pipeline", kill)
    monkeypatch.setattr(rec.ring, "low_disk", lambda: None)
    rec._procs = [types.SimpleNamespace(returncode=127, pid=1)]
    rec._run_start_ms = 0  # spawned long ago
    asyncio.run(rec.check_once())
    assert events == ["kill", "spawn"]


def test_recorder_alerts_once_when_recording_falls_behind_live(tmp_path, monkeypatch):
    import asyncio

    from clipwatcher.ingest.recorder import Recorder

    alerts = []

    async def on_alert(text):
        alerts.append(text)

    rec = Recorder("twitch", "x", "s", tmp_path, on_alert=on_alert)
    lag = {"s": 840.0}
    monkeypatch.setattr(rec, "newest_segment_age_s", lambda now=None: lag["s"])

    async def go():
        await rec._check_lag()
        await rec._check_lag()  # still behind: no second alert
        lag["s"] = 5.0
        await rec._check_lag()  # caught up: re-armed
        lag["s"] = 300.0
        await rec._check_lag()

    asyncio.run(go())
    assert len(alerts) == 2 and "14.0 min behind live" in alerts[0]
