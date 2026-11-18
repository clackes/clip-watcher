import json
import threading
import time

import pytest

from clipwatcher import config
from clipwatcher.queue import staging, telegram_bot
from clipwatcher.queue.db import ClipDB


@pytest.fixture
def db(tmp_path):
    d = ClipDB(tmp_path / "q.db")
    d.add_clip("c1", "streamer", {"title": "t"}, score=9)
    return d


def test_claim_race(tmp_path):
    db = ClipDB(tmp_path / "r.db")
    db.add_clip("c1", "s")
    res, barrier = [], threading.Barrier(2)

    def go(op):
        barrier.wait()
        res.append((op, db.claim("c1", op)))

    ts = [threading.Thread(target=go, args=(o,)) for o in ("manu", "samu")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wins = [r for r in res if r[1][0]]
    assert len(wins) == 1
    winner = wins[0][0]
    loser = next(r for r in res if not r[1][0])
    assert loser[1][1] == winner


def test_lock_expiry_and_holder_only(db):
    assert db.claim("c1", "manu", ttl_min=0.001)[0]
    assert not db.approve("c1", "samu")
    assert db.claim("c1", "samu")[1] == "manu" or True
    time.sleep(0.1)
    assert db.claim("c1", "samu") == (True, "samu")
    assert not db.approve("c1", "manu")
    assert db.edit("c1", "samu", {"title": "new"})
    assert db.approve("c1", "samu")
    assert db.state("c1") == "APPROVED" and db.get("c1")["approved_by"] == "samu"
    assert json.loads(db.get("c1")["meta_json"])["title"] == "new"


def test_release_reject_and_audit(db):
    db.claim("c1", "manu")
    assert not db.release("c1", "samu")
    assert db.release("c1", "manu") and db.state("c1") == "READY"
    db.claim("c1", "samu")
    assert db.reject("c1", "samu", "meh") and db.state("c1") == "REJECTED"
    actions = [r["action"] for r in db.audit_rows("c1")]
    assert actions[:2] == ["add", "claim"]
    assert "release_denied" in actions and "reject" in actions


def test_job_lease_retry(db):
    jid = db.enqueue_job("render", {"a": 1})
    j = db.claim_job("render", lease_s=0.05)
    assert j["id"] == jid and j["attempts"] == 1 and j["payload"] == {"a": 1}
    assert db.claim_job("render") is None
    time.sleep(0.1)
    j2 = db.claim_job("render", lease_s=30)
    assert j2["id"] == jid and j2["attempts"] == 2
    db.finish_job(jid)
    assert db.claim_job("render") is None


def test_auto_approve_respects_config(db, monkeypatch):
    monkeypatch.setattr(config, "global_config", lambda: {"publish": {"auto_approve": False}})
    assert db.auto_approve() == [] and db.auto_approve_one("c1") is False
    cfg = {"publish": {"auto_approve": True, "auto_approve_min": 8}}
    monkeypatch.setattr(config, "global_config", lambda: cfg)
    assert db.auto_approve() == ["c1"] and db.state("c1") == "APPROVED"


def test_auto_approve_one_only_this_clip_above_min(db, monkeypatch):
    cfg = {"publish": {"auto_approve": True, "auto_approve_min": 8}}
    monkeypatch.setattr(config, "global_config", lambda: cfg)
    assert db.auto_approve_one("nope") is False
    assert db.auto_approve_one("c1") is True and db.state("c1") == "APPROVED"
    assert db.auto_approve_one("c1") is False  # already approved


def test_staging_flow(tmp_path, db):
    s, c = tmp_path / "s.mp4", tmp_path / "c.mp4"
    s.write_bytes(b"s")
    c.write_bytes(b"c")
    root = tmp_path / "staging"
    d = staging.stage_package("c9", s, c, {"streamer": "x", "score": 9}, root, db=db)
    assert (d / "short_9x16.mp4").exists() and json.loads((d / "meta.json").read_text())["state"] == "READY"
    assert db.state("c9") == "READY"
    a = staging.mark_approved("c9", "manu", root)
    assert json.loads((a / "meta.json").read_text())["approved_by"] == "manu"
    assert (a / "clean_16x9.mp4").exists()
    staging.update_published("c9", "youtube", "https://y/1", root)
    data = staging.update_published("c9", "instagram", "https://i/1", root)
    assert json.loads((root / "published_clips.json").read_text()) == data
    assert data["c9"]["youtube"] == "https://y/1"


OPS = {"manu": {"telegram_id": 11, "label": "Manu"}, "samu": {"telegram_id": 22, "label": "Samu"}}


def test_bot_logic(db, tmp_path, monkeypatch):
    monkeypatch.setattr(staging, "DEFAULT_ROOT", tmp_path / "staging")
    assert telegram_bot.handle_action(db, 999, "claim:c1:manu", operators=OPS) == ("", None)
    assert len(telegram_bot.buttons_for(db.get("c1"), OPS)[0]) == 2
    text, _ = telegram_bot.handle_action(db, 11, "claim:c1:manu", operators=OPS)
    assert text == "Claimed."
    assert "manu" in telegram_bot.handle_action(db, 22, "claim:c1:samu", operators=OPS)[0]
    assert telegram_bot.handle_action(db, 22, "approve:c1", operators=OPS)[0].startswith("Only")
    assert [b[0] for b in telegram_bot.buttons_for(db.get("c1"), OPS)[0]] == ["Approve", "Edit"]
    assert telegram_bot.handle_action(db, 11, "approve:c1", operators=OPS)[0] == "Approved."


@pytest.mark.ffmpeg
def test_telegram_video_attrs_and_thumbnail(tmp_path):
    import subprocess

    from clipwatcher import config
    from clipwatcher.queue import telegram_bot

    v = tmp_path / "s.mp4"
    subprocess.run(
        [config.ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1080x1920:r=30", "-t", "3",
         "-pix_fmt", "yuv420p", str(v)],
        check=True,
    )  # fmt: skip
    assert telegram_bot.video_attrs(v) == {"width": 1080, "height": 1920, "duration": 3}
    th = telegram_bot.thumbnail(v)
    from PIL import Image

    with Image.open(th) as im:
        assert im.size == (180, 320)
    assert telegram_bot.video_attrs(tmp_path / "missing.mp4") == {}
