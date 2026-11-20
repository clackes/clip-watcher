"""Progress events: what a lane tells the companion app while it runs."""

import asyncio
import json

import pytest

from clipwatcher import events, fastlane
from clipwatcher.models import ChatMessage
from tests.test_fastlane import FakeChat, FakeDetector, FakeRing, _steps


def _read(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_nothing_is_written_unless_someone_watches(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIPWATCHER_EVENTS", raising=False)
    events.emit("moment", clip_id="x")  # a bot started from a terminal: no file, no error
    with events.step("cut"):
        pass
    assert list(tmp_path.iterdir()) == []


def test_events_are_json_lines_and_a_step_reports_how_it_ended(tmp_path, monkeypatch):
    out = tmp_path / "events.jsonl"
    monkeypatch.setenv("CLIPWATCHER_EVENTS", str(out))
    events.emit("recording", streamer="zack", ring=tmp_path)  # paths and other objects are written as text
    with events.step("cut", streamer="zack"):
        pass
    with pytest.raises(ValueError), events.step("render_short", streamer="zack"):
        raise ValueError("ffmpeg failed")
    rows = _read(out)
    assert [(r["kind"], r.get("step"), r.get("state")) for r in rows] == [
        ("recording", None, None), ("step", "cut", "start"), ("step", "cut", "done"),
        ("step", "render_short", "start"), ("step", "render_short", "failed"),
    ]  # fmt: skip
    assert (
        rows[0]["ring"] == str(tmp_path) and rows[2]["seconds"] >= 0 and "ffmpeg failed" in rows[4]["error"]
    )
    monkeypatch.setenv("CLIPWATCHER_EVENTS", str(tmp_path / "gone" / "events.jsonl"))
    events.emit("moment")  # a folder that is not there must not stop a clip


def test_the_live_lane_reports_a_clip_from_the_moment_to_the_staged_package(tmp_path, monkeypatch):
    out = tmp_path / "events.jsonl"
    monkeypatch.setenv("CLIPWATCHER_EVENTS", str(out))
    log: list = []
    t0 = 1_700_000_000_000
    lane = fastlane.FastLane(
        "zack", FakeChat([ChatMessage(t0, "u", "fire")]), FakeDetector({"fire"}), FakeRing(log),
        steps=_steps(log), cfg={"work_dir": str(tmp_path / "work")},
    )  # fmt: skip
    assert [o.status for o in asyncio.run(lane.run())] == ["staged"]
    rows = _read(out)
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "watching" and kinds.index("moment") < kinds.index("judged") < kinds.index("staged")
    done = [r["step"] for r in rows if r["kind"] == "step" and r["state"] == "done"]
    assert done[:2] == ["wait", "cut"] and {"render_short", "render_clean", "stage"} <= set(done)
    staged = rows[kinds.index("staged")]
    assert staged["clip_id"].startswith("zack-live-") and staged["timings"]["cut"] >= 0 and staged["title"]
    assert all(r["streamer"] == "zack" for r in rows)
