import asyncio
from pathlib import Path

from clipwatcher import fastlane
from clipwatcher.models import Candidate, ChatMessage, JudgeResult, Word


class FakeChat:
    def __init__(self, msgs):
        self.msgs = msgs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self.msgs:
            yield m
            await asyncio.sleep(0)


class FakeDetector:
    def __init__(self, fire_on):
        self.fire_on = fire_on

    def feed(self, msg):
        if msg.text in self.fire_on:
            t = msg.ts_ms / 1000
            return Candidate("zack", t - 10, t - 30, t + 10, 5.0, 6.0)
        return None


class FakeRing:
    def __init__(self, log):
        self.log = log

    def pin(self, s, e, ttl):
        self.log.append(("pin", s, e, ttl))

    def unpin(self, s, e):
        self.log.append(("unpin", s, e))

    def covered_until_ms(self):
        self.log.append(("covered",))
        return 10**15

    def cut(self, s, e, out):
        self.log.append(("cut", s, e))
        Path(out).write_bytes(b"x")
        return Path(out), 3.5  # real RingBuffer.cut returns (path, local_start_s)


def _steps(log):
    def tr(src, st, dur, lang):
        log.append(("transcribe", dur))
        return [Word(0, 1, "wow")]

    def sc(cand, words, chat, title, game):
        log.append(("screen", list(chat)))
        return {"funny_score": 7, "hype_score": 7, "reject_reason": None}

    def heur(cand, words):
        return JudgeResult(7, 7, "", 0, 1, "NO WAY", ["t"], "s", source="heuristic")

    def rs(src, st, dur, out, **kw):
        log.append(("render_short", kw["profile"], kw["hook"]))
        log.append(("render_start", st))
        Path(out).write_bytes(b"s")
        return Path(out)

    def rc(src, st, dur, out, **kw):
        log.append(("render_clean", kw["profile"]))
        Path(out).write_bytes(b"c")
        return Path(out)

    def stage(cid, short, clean, meta, **kw):
        log.append(("stage", cid, meta["lane"]))
        return Path("/staged") / cid

    async def post(app, db, cid, video):
        log.append(("post", cid))

    return fastlane.Steps(tr, sc, heur, rs, rc, stage, post)


def test_sequence_and_pins(tmp_path):
    log: list = []
    t0 = 1_700_000_000_000
    msgs = [ChatMessage(t0 + i * 1000, f"u{i}", "KEKW" if i == 40 else "hi") for i in range(60)]
    msgs[41] = ChatMessage(t0 + 41_000, "late", "fire")
    lane = fastlane.FastLane(
        "zack", FakeChat(msgs), FakeDetector({"fire"}), FakeRing(log), steps=_steps(log),
        cfg={"work_dir": str(tmp_path), "pin_pad_s": 60}, db=object(), bot=object(),
    )  # fmt: skip
    out = asyncio.run(lane.run())
    assert [o.status for o in out] == ["staged"]
    names = [e[0] for e in log if e[0] not in ("covered", "render_start")]
    assert names == [
        "pin",
        "pin",
        "cut",
        "transcribe",
        "screen",
        "render_short",
        "render_clean",
        "stage",
        "post",
        "unpin",
    ]
    peak = t0 / 1000 + 41
    pin = next(e for e in log if e[0] == "pin")
    assert pin[1] == int((peak - 30 - 60) * 1000) and pin[2] == int((peak + 10 + 60) * 1000)
    assert pin[3] == 48 * 3600
    cut = next(e for e in log if e[0] == "cut")
    assert cut[2] - cut[1] == (40 + 5) * 1000  # window plus post-roll
    # cut's local offset (3.5) + late start: no judge, no audio -> reaction = chat peak (20 s in) - 3 s lag,
    # clip starts 10 s before it
    assert ("render_start", 3.5 + 7.0) in log
    assert ("render_short", "cpu_live", "NO WAY") in log and ("render_clean", "cpu_live") in log
    assert set(out[0].timings) >= {
        "cut",
        "transcribe",
        "screen",
        "render_short",
        "render_clean",
        "stage",
        "total",
    }
    assert any("late: fire" in str(e) for e in log if e[0] == "screen")


def test_reject_skips_render(tmp_path):
    log: list = []
    st = _steps(log)
    st.screen = lambda *a: {"funny_score": 1, "hype_score": 1, "reject_reason": "dead air"}
    msgs = [ChatMessage(1_700_000_000_000, "a", "fire")]
    lane = fastlane.FastLane(
        "zack",
        FakeChat(msgs),
        FakeDetector({"fire"}),
        FakeRing(log),
        steps=st,
        cfg={"work_dir": str(tmp_path)},
    )
    out = asyncio.run(lane.run())
    assert out[0].status == "rejected"
    assert not any(e[0] == "render_short" for e in log)


def test_bounded_concurrency(tmp_path):
    log: list = []
    active = {"n": 0, "max": 0}
    st = _steps(log)

    def slow_tr(*a):
        import time

        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        time.sleep(0.05)
        active["n"] -= 1
        return []

    st.transcribe = slow_tr
    msgs = [ChatMessage(1_700_000_000_000 + i * 200_000, "a", "fire") for i in range(5)]
    lane = fastlane.FastLane(
        "zack", FakeChat(msgs), FakeDetector({"fire"}), FakeRing(log), steps=st,
        cfg={"work_dir": str(tmp_path), "max_parallel": 2},
    )  # fmt: skip
    out = asyncio.run(lane.run())
    assert len(out) == 5 and active["max"] <= 2


def test_wall_clock_wait_when_ring_has_no_coverage_api(tmp_path):
    log: list = []
    ring = FakeRing(log)
    ring.covered_until_ms = None  # type: ignore[assignment]
    now = [0.0]
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        now[0] += s

    cand = Candidate("zack", 100, 90, 110, 5, 6)
    now[0] = 100.0
    lane = fastlane.FastLane(
        "zack", FakeChat([]), FakeDetector(set()), ring, steps=_steps(log),
        cfg={"work_dir": str(tmp_path), "segment_s": 10}, clock=lambda: now[0], sleep=sleep,
    )  # fmt: skip
    asyncio.run(lane._wait_for_buffer(cand.end_s + 5))
    assert now[0] >= 125.0


def test_global_slots_are_shared_by_every_lane_in_a_loop():
    import asyncio

    from clipwatcher.fastlane import global_slots

    async def go():
        a, b = global_slots(2), global_slots(5)
        assert a is b  # one semaphore per event loop, whatever the lanes ask for later
        running, peak = 0, 0

        async def job():
            nonlocal running, peak
            async with global_slots(2):
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.01)
                running -= 1

        await asyncio.gather(*(job() for _ in range(6)))
        return peak

    assert asyncio.run(go()) == 2


def test_same_window_fires_once(tmp_path):
    log: list = []
    t0 = 1_700_000_000_000
    msgs = [ChatMessage(t0, "a", "fire"), ChatMessage(t0, "b", "fire")]  # detector fires twice, same window
    lane = fastlane.FastLane(
        "zack", FakeChat(msgs), FakeDetector({"fire"}), FakeRing(log), steps=_steps(log),
        cfg={"work_dir": str(tmp_path)},
    )  # fmt: skip
    out = asyncio.run(lane.run())
    assert len(out) == 1 and sum(e[0] == "transcribe" for e in log) == 1


def test_clip_in_flight_is_finished_when_chat_crashes(tmp_path):
    """A chat error ended run() with the clip task still running; the caller then stopped the recorder and a
    restart deleted the ring the clip was about to cut from."""
    log: list = []
    t0 = 1_700_000_000_000

    class CrashingChat:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield ChatMessage(t0, "u", "fire")
            raise ConnectionError("chat source died")

    lane = fastlane.FastLane(
        "zack", CrashingChat(), FakeDetector({"fire"}), FakeRing(log), steps=_steps(log),
        cfg={"work_dir": str(tmp_path)}, db=object(), bot=object(),
    )  # fmt: skip
    try:
        asyncio.run(lane.run())
    except ConnectionError:
        pass
    else:
        raise AssertionError("the crash must still reach the supervisor")
    assert [o.status for o in lane.outcomes] == ["staged"] and any(e[0] == "stage" for e in log)


def test_confirmation_raises_the_final_score_and_reaches_the_card(tmp_path):
    """D3: loudness where chat spiked and a viewer clip of the moment were computed nowhere."""
    log: list = []
    staged = {}
    t0 = 1_700_000_000_000
    steps = _steps(log)
    # the window is 40 s, the moment (peak) 20 s in: quiet talk at -30 dB with a scream to -8 dB at 21-22 s
    steps.loudness = lambda src, st, dur: [(i / 10, -8.0 if 210 <= i < 220 else -30.0) for i in range(400)]
    steps.stage = lambda cid, short, clean, meta, **kw: staged.update(meta) or Path("/staged") / cid

    class Clips:
        def moment_times(self):
            return [t0 / 1000 - 10 + 12]  # a viewer clipped 12 s from the peak

    lane = fastlane.FastLane(
        "zack", FakeChat([ChatMessage(t0, "u", "fire")]), FakeDetector({"fire"}), FakeRing(log), steps=steps,
        cfg={"work_dir": str(tmp_path)}, clips=Clips(),
    )  # fmt: skip
    assert [o.status for o in asyncio.run(lane.run())] == ["staged"]
    assert staged["viewer_clip"] is True and staged["loud_z"] > 5
    assert (
        staged["chat_score"] == 5.0 and staged["final_score"] == 5.0 + 1.5 + 5 * 0.3
    )  # clip + capped loudness


def test_chat_of_the_moment_is_taken_when_it_fires(tmp_path):
    """A clip that waited for a render slot read the chat buffer minutes later, after the moment's lines had
    been pruned from it, and the judge got no chat."""
    log: list = []
    t0 = 1_700_000_000_000
    msgs = [ChatMessage(t0 + i * 1000, f"u{i}", "KEKW") for i in range(20)]
    msgs.append(ChatMessage(t0 + 20_000, "late", "fire"))
    msgs += [ChatMessage(t0 + 21_000 + i * 60_000, "x", "later") for i in range(1, 30)]  # half an hour passes
    lane = fastlane.FastLane(
        "zack", FakeChat(msgs), FakeDetector({"fire"}), FakeRing(log), steps=_steps(log),
        cfg={"work_dir": str(tmp_path), "max_parallel": 1},
    )  # fmt: skip
    asyncio.run(lane.run())
    seen = next(e[1] for e in log if e[0] == "screen")
    assert sum("KEKW" in line for line in seen) == 20


def test_quiet_chat_is_ticked_and_the_last_spike_is_drained(tmp_path):
    log: list = []
    t0 = 1_700_000_000_000

    class SlowDetector:
        """Decides only when its clock moves (tick) or at the end (drain), like LiveDetector."""

        def __init__(self):
            self.ticks = []

        def feed(self, msg):
            return None

        def tick(self, now_s):
            self.ticks.append(now_s)
            return (
                Candidate("zack", now_s - 10, now_s - 30, now_s + 10, 5.0, 6.0)
                if len(self.ticks) == 1
                else None
            )

        def drain(self):
            return [Candidate("zack", 500.0, 480.0, 520.0, 7.0, 7.0)]

    class QuietChat:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield ChatMessage(t0, "u", "the moment")
            await asyncio.sleep(1.3)  # nobody types; the lane's ticker runs once meanwhile

    now = iter([1000.0])  # our clock when the message arrived; every later reading is 10 s on
    det = SlowDetector()
    lane = fastlane.FastLane(
        "zack", QuietChat(), det, FakeRing(log), steps=_steps(log), cfg={"work_dir": str(tmp_path)},
        clock=lambda: next(now, 1010.0),
    )  # fmt: skip
    out = asyncio.run(lane.run())
    assert det.ticks == [t0 / 1000 + 10.0 - 5.0]  # chat time: last message + the silence, less the margin
    assert len(out) == 2 and {o.status for o in out} == {"staged"}  # the ticked spike and the drained one
