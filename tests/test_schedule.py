import asyncio

from clipwatcher.queue import schedule
from clipwatcher.queue.db import ClipDB

CFG = {"enabled": True, "max_per_day": 6, "min_gap_min": 180, "min_score": 6, "max_age_h": 24}


def _db(tmp_path, now):
    db = ClipDB(tmp_path / "c.db")
    clips = [
        ("ok7", 7, "claude", now - 3600),
        ("best9", 9, "claude", now - 3600),
        ("heur9", 9, "heuristic", now - 600),  # never unattended
        ("low5", 5, "claude", now - 600),
        ("old10", 10, "claude", now - 30 * 3600),
    ]
    for cid, score, src, created in clips:
        db.add_clip(cid, "kaicenat", {"judge_source": src, "streamer": "kaicenat"}, score)
        db._conn().execute("UPDATE clips SET created_at=? WHERE id=?", (created, cid))
    return db


def _publish(db, cid, ts):
    db.set_state(cid, "PUBLISHED", actor="publisher", youtube_id="x")
    db._conn().execute("UPDATE audit SET ts=? WHERE clip_id=? AND action='state:PUBLISHED'", (ts, cid))


def test_pick_best_judged_fresh_clip(tmp_path):
    now = 1_800_000_000.0
    db = _db(tmp_path, now)
    assert schedule.pick(db, now, CFG) == "best9"
    assert schedule.pick(db, now, {**CFG, "enabled": False}) is None


def test_gap_and_daily_cap(tmp_path):
    now = 1_800_000_000.0
    db = _db(tmp_path, now)
    _publish(db, "best9", now - 3600)  # 1 h ago: too soon
    assert schedule.pick(db, now, CFG) is None
    assert schedule.pick(db, now + 2 * 3600, CFG) == "ok7"
    for i in range(5):  # 6 in the last 24 h (operator uploads count too)
        db.add_clip(f"m{i}", "x", {}, 8)
        _publish(db, f"m{i}", now - 20 * 3600 + i)
    assert schedule.pick(db, now + 2 * 3600, CFG) is None
    assert schedule.pick(db, now + 5 * 3600, CFG) == "ok7"  # the oldest ones left the 24 h window


def test_run_approves_then_uploads(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    db = _db(tmp_path, now)
    monkeypatch.setattr(schedule, "settings", lambda: CFG)
    marked, sent = [], []
    from clipwatcher.queue import staging

    monkeypatch.setattr(staging, "mark_approved", lambda cid, by: marked.append((cid, by)))

    async def upload(cid, n, cap):
        sent.append((cid, db.state(cid), n, cap))
        return "https://youtu.be/x"

    asyncio.run(schedule.run(db, upload, clock=lambda: now, once=True))
    assert marked == [("best9", "auto")] and sent == [("best9", "APPROVED", 1, 6)]


def test_failed_upload_returns_the_clip_and_pauses(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    db = _db(tmp_path, now)
    monkeypatch.setattr(schedule, "settings", lambda: CFG)
    from clipwatcher.queue import staging

    monkeypatch.setattr(staging, "mark_approved", lambda cid, by: None)
    slept: list = []

    async def upload(cid, n, cap):
        return None  # YouTube signed out

    async def sleep(s):
        slept.append(s)
        raise asyncio.CancelledError

    try:
        asyncio.run(schedule.run(db, upload, clock=lambda: now, sleep=sleep))
    except asyncio.CancelledError:
        pass
    assert db.state("best9") == "READY" and slept == [3600]
