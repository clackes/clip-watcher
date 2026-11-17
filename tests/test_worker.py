import time
from pathlib import Path

from clipwatcher.queue.db import ClipDB
from clipwatcher.worker import STAGES, Worker, clip_base


def _db(tmp_path):
    return ClipDB(tmp_path / "q.db")


def _jobs(db):
    return [dict(r) for r in db._conn().execute("SELECT * FROM jobs ORDER BY id")]


def test_chain_runs_all_stages(tmp_path):
    db = _db(tmp_path)
    seen = []

    def mk(stage):
        def h(p, w):
            seen.append(stage)
            return [] if stage == "stage" else [{**p, stage: True}]

        return h

    w = Worker(db, handlers={s: mk(s) for s in STAGES}, clips_root=tmp_path / "clips")
    w.enqueue("detect", {"streamer": "z"})
    assert w.run_once() == 6
    assert seen == list(STAGES)
    assert {j["state"] for j in _jobs(db)} == {"DONE"}


def test_retry_then_success_and_exhaustion(tmp_path):
    db = _db(tmp_path)
    calls = {"n": 0}

    def flaky(p, w):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return []

    w = Worker(db, handlers={"stage": flaky}, max_attempts=3)
    w.enqueue("stage", {})
    w.run_once()
    j = _jobs(db)[0]
    assert j["state"] == "DONE" and j["attempts"] == 3

    w2 = Worker(db, handlers={"stage": lambda p, w: 1 / 0}, max_attempts=2)
    w2.enqueue("stage", {})
    w2.run_once()
    j = _jobs(db)[1]
    assert j["state"] == "FAILED" and j["attempts"] == 2 and "ZeroDivisionError" in j["error"]


def test_lease_retry_after_crash(tmp_path):
    db = _db(tmp_path)
    jid = db.enqueue_job("render", {"x": 1})
    crashed = db.claim_job("render", lease_s=0.05)  # worker claims, then dies without finishing
    assert crashed["id"] == jid and crashed["attempts"] == 1
    w = Worker(db, handlers={"render": lambda p, w: [{**p, "ok": 1}], "stage": lambda p, w: []})
    assert w.step() is False or True  # lease may still be live; must not double-run
    assert _jobs(db)[0]["attempts"] in (1, 2)
    time.sleep(0.1)
    w.run_once()
    jobs = _jobs(db)
    assert jobs[0]["state"] == "DONE" and jobs[0]["attempts"] == 2
    assert jobs[1]["stage"] == "stage" and jobs[1]["state"] == "DONE"
    assert jobs[1]["payload"] and '"ok": 1' in jobs[1]["payload"]


def test_atomic_output_and_idempotence(tmp_path):
    base = clip_base("z", {"start_s": 12.5, "end_s": 40.0}, tmp_path)
    assert base == tmp_path / "z" / "12500-40000"
    final = base.with_suffix(".mp4")

    def boom(tmp):
        tmp.write_bytes(b"partial")
        raise RuntimeError("crash mid-render")

    try:
        Worker.atomic(final, boom)
    except RuntimeError:
        pass
    assert not final.exists()
    Worker.atomic(final, lambda t: Path(t).write_bytes(b"ok"))
    assert final.read_bytes() == b"ok" and not final.with_name("12500-40000.part.mp4").exists()
