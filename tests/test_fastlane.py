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

    st = fastlane.Steps(tr, sc, heur, rs, rc, stage, post)
    # Never the real judge: with ANTHROPIC_API_KEY in the environment (the server's .env) the default judge
    # extracted frames and could call the API from a unit test.
    st.judge = lambda *a, **k: None
    st.still_edges = lambda *a, **k: (0.0, 0.0)
    return st


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
        "pin",  # kept for Telegram's Redo
    ]
    assert [e for e in log if e[0] == "pin"][-1][3] == fastlane.REDO_KEEP_S
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


def test_judge_gets_the_chat_that_reacts_to_the_clip():
    """Regression (18 Nov): chat runs delay_s behind the video; the judge got the clip's own seconds of chat,
    i.e. the calm before the reaction, and called every GTA VI moment "chat quiet"."""
    import types

    from clipwatcher.models import Candidate, ChatMessage

    lane = fastlane.FastLane("x", None, types.SimpleNamespace(delay_s=8.0), None, cfg={"post_roll_s": 5.0})
    for t in range(100, 160):
        lane.recent.append(ChatMessage(t * 1000, f"u{t}", "before" if t < 128 else "LMAO"))
    cand = Candidate("x", 120.0, 100.0, 130.0, chat_score=6, final_score=6)  # chat reacts at 128-143
    lines = lane._chat_lines(cand)
    assert sum("LMAO" in ln for ln in lines) == 143 - 128 + 1
    assert all(int(ln.split(": ")[0][1:]) >= 108 for ln in lines)


def test_keep_rejected_writes_review(tmp_path):
    import json

    log: list = []
    st = _steps(log)
    st.screen = lambda *a: {"funny_score": 1, "hype_score": 1, "reject_reason": "dead air"}
    msgs = [ChatMessage(1_700_000_000_000, "a", "fire")]
    lane = fastlane.FastLane(
        "zack", FakeChat(msgs), FakeDetector({"fire"}), FakeRing(log), steps=st,
        cfg={"work_dir": str(tmp_path), "keep_rejected": True},
    )  # fmt: skip
    out = asyncio.run(lane.run())
    assert out[0].status == "rejected"
    review = json.loads((tmp_path / out[0].clip_id / "review.json").read_text(encoding="utf-8"))
    assert review["screen"]["reject_reason"] == "dead air" and review["candidate"]["stream"] == "zack"
    assert review["words"][0]["text"] == "wow"


def test_card_error_keeps_the_clip_staged(tmp_path):
    """Regression (18 Nov): the Telegram upload timed out after the card arrived and the clip was logged
    as failed. A card error is logged; the clip stays staged."""
    log: list = []
    t0 = 1_700_000_000_000
    msgs = [ChatMessage(t0 + i * 1000, f"u{i}", "hi") for i in range(60)]
    msgs[41] = ChatMessage(t0 + 41_000, "late", "fire")
    st = _steps(log)

    async def boom(*a, **k):
        raise TimeoutError("Timed out")

    st.post_card = boom
    lane = fastlane.FastLane(
        "zack", FakeChat(msgs), FakeDetector({"fire"}), FakeRing(log), steps=st,
        cfg={"work_dir": str(tmp_path), "pin_pad_s": 60}, db=object(), bot=object(),
    )  # fmt: skip
    out = asyncio.run(lane.run())
    assert [o.status for o in out] == ["staged"]
    assert any(e[0] == "unpin" for e in log)


def _one_clip(tmp_path, st, **kw):
    t0 = 1_700_000_000_000
    msgs = [ChatMessage(t0 + i * 1000, f"u{i}", "hi") for i in range(60)]
    msgs[41] = ChatMessage(t0 + 41_000, "late", "fire")
    lane = fastlane.FastLane(
        "zack", FakeChat(msgs), FakeDetector({"fire"}), FakeRing([]), steps=st,
        cfg={"work_dir": str(tmp_path), "pin_pad_s": 60}, **kw,
    )  # fmt: skip
    return asyncio.run(lane.run())


def test_still_head_is_cut_before_render(tmp_path):
    """Owner, 18 Nov: a clip opened on 2 s of a capture card's NO SIGNAL screen."""
    base: list = []
    _one_clip(tmp_path / "a", _steps(base))
    cut: list = []
    st = _steps(cut)
    st.still_edges = lambda *a, **k: (2.0, 0.0)
    _one_clip(tmp_path / "b", st)
    start = lambda lg: next(e[1] for e in lg if e[0] == "render_start")  # noqa: E731
    assert round(start(cut) - start(base), 2) == 2.0


def test_auto_publish_runs_after_staging(tmp_path):
    log: list = []
    st = _steps(log)

    async def auto(app, db, cid):
        log.append(("auto", cid))
        return "https://youtube.com/shorts/x"

    st.auto_publish = auto
    out = _one_clip(tmp_path, st, db=object(), bot=object())
    assert [o.status for o in out] == ["staged"]
    assert any(e[0] == "auto" for e in log)
