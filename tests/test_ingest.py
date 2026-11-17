import asyncio
import subprocess

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


def test_prune_disk_low(tmp_path, monkeypatch):
    rb = RingBuffer(tmp_path / "r", window_min=1, min_free_disk_pct=15)
    _fill(rb, 5)
    monkeypatch.setattr("clipwatcher.ingest.ringbuffer.free_pct", lambda p: 5.0)
    res = rb.prune(now_ms=9_999_999)
    assert res.alert and not res.deleted and len(rb.segments()) == 5


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
    (rec.dir / "seg_a_00001.ts").write_bytes(b"")
    (rec.dir / "a.csv").write_text("seg_a_00000.ts,0.0,10.0\nseg_a_00001.ts,10.0,20.0\n")
    assert rec.ingest_run("a", 5_000_000) == 1
    assert rec.ring.segments()[0]["start_ms"] == 5_000_000 and not (rec.dir / "seg_a_00001.ts").exists()
    assert rec.newest_segment_age_s(now=5_020) is not None


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
