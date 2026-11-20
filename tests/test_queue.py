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
    time.sleep(0.2)
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


def _videos(tmp_path):
    s, c = tmp_path / "s.mp4", tmp_path / "c.mp4"
    s.write_bytes(b"short")
    c.write_bytes(b"clean")
    return s, c


def test_restaging_never_resets_a_decided_clip(tmp_path, db, monkeypatch):
    """A VOD re-run stages the same clip ids again: only a clip that is still READY may be replaced."""
    from clipwatcher.publish import youtube

    monkeypatch.setenv("DRY_RUN", "1")
    root = tmp_path / "staging"
    s, c = _videos(tmp_path)
    staging.stage_package("v1", s, c, {"streamer": "x", "title": "first"}, root, db=db)
    link = tmp_path / "link.mp4"
    link.hardlink_to(root / "v1" / "short_9x16.mp4")
    s.write_bytes(b"short v2")
    staging.stage_package("v1", s, c, {"streamer": "x", "title": "second", "score": 7}, root, db=db)
    row = db.get("v1")
    assert row["state"] == "READY" and row["score"] == 7 and json.loads(row["meta_json"])["title"] == "second"
    assert (root / "v1" / "short_9x16.mp4").read_bytes() == b"short v2"
    assert link.read_bytes() == b"short"  # replaced by rename, never written into the shared inode

    db.claim("v1", "manu")
    db.approve("v1", "manu")
    staging.mark_approved("v1", "manu", root)
    db.set_state("v1", "PUBLISHED", actor="publisher", youtube_id="yt1")
    s.write_bytes(b"short v3")
    assert staging.stage_package("v1", s, c, {"streamer": "x", "title": "rerun"}, root, db=db) == root / "v1"
    row = db.get("v1")
    assert (row["state"], row["youtube_id"], row["approved_by"]) == ("PUBLISHED", "yt1", "manu")
    assert json.loads(row["meta_json"])["title"] == "second"
    assert (root / "approved" / "v1" / "short_9x16.mp4").read_bytes() == b"short v2"
    assert (root / "v1" / "short_9x16.mp4").read_bytes() == b"short v2"
    assert json.loads((root / "v1" / "meta.json").read_text())["state"] == "APPROVED"
    assert "restage_refused" in [r["action"] for r in db.audit_rows("v1")]
    with pytest.raises(youtube.NotApproved):  # no second approve, so no second upload
        youtube.publish_clip(db, "v1", root)

    for decided in ("claim", "approve", "reject"):
        cid = f"v-{decided}"
        staging.stage_package(cid, s, c, {"streamer": "x"}, root, db=db)
        db.claim(cid, "samu")
        if decided != "claim":
            getattr(db, decided)(cid, "samu")
        before = db.get(cid)
        assert db.add_clip(cid, "x", {"title": "again"}) is False
        staging.stage_package(cid, s, c, {"streamer": "x"}, root, db=db)
        assert db.get(cid) == before


OPS = {"manu": {"telegram_id": 11, "label": "Manu"}, "samu": {"telegram_id": 22, "label": "Samu"}}


def test_approved_package_carries_the_edited_title(tmp_path, db):
    """The title an operator typed in Telegram lives in the queue row; approved/meta.json is what the people
    posting to TikTok and the campaign forms read."""
    root = tmp_path / "staging"
    s, c = _videos(tmp_path)
    staging.stage_package("c9", s, c, {"streamer": "x", "title": "judge title", "hook": "h"}, root, db=db)
    telegram_bot.handle_action(db, 11, "claim:c9:manu", operators=OPS, root=root)
    assert telegram_bot.handle_edit_reply(db, 11, "c9", " Operator title ", operators=OPS)
    assert telegram_bot.handle_action(db, 11, "approve:c9", operators=OPS, root=root)[0] == "Approved."
    for folder in (root / "approved" / "c9", root / "c9"):
        meta = json.loads((folder / "meta.json").read_text())
        assert (meta["title"], meta["hook"], meta["state"], meta["approved_by"]) == (
            "Operator title", "h", "APPROVED", "manu",
        )  # fmt: skip


def test_bot_logic(db, tmp_path, monkeypatch):
    monkeypatch.delenv("CLIPWATCHER_STAGING", raising=False)
    monkeypatch.setattr(staging, "DEFAULT_ROOT", tmp_path / "staging")
    assert telegram_bot.handle_action(db, 999, "claim:c1:manu", operators=OPS) == ("", None)
    rows = telegram_bot.buttons_for(db.get("c1"), OPS)
    assert len(rows[0]) == 2 and rows[1] == [("🔁 Redo", "redo:c1")]
    text, _ = telegram_bot.handle_action(db, 11, "claim:c1:manu", operators=OPS)
    assert text == "Claimed."
    assert "manu" in telegram_bot.handle_action(db, 22, "claim:c1:samu", operators=OPS)[0]
    refused = telegram_bot.handle_action(db, 22, "approve:c1", operators=OPS)[0]
    assert refused == "Only the holder can approve."
    labels = [b[0] for r in telegram_bot.buttons_for(db.get("c1"), OPS) for b in r]
    assert labels[:2] == ["Approve", "Edit"] and "🔁 Redo" in labels
    assert "Held by: manu" in telegram_bot.card_text(db.get("c1"))
    assert telegram_bot.handle_action(db, 11, "approve:c1", operators=OPS)[0] == "Approved."


def test_bot_redo_queues_a_job_for_the_streamer_lane(db):
    assert telegram_bot.handle_action(db, 11, "redo:c1", operators=OPS)[0] == telegram_bot.REDO_ASK
    msg = telegram_bot.request_redo(db, 11, "c1", "  start earlier ", operators=OPS)
    assert msg.startswith("🔁")
    streamer = db.get("c1")["streamer"]
    job = db.claim_job(f"redo:{streamer}")
    assert job["payload"] == {"clip_id": "c1", "operator": "manu", "note": "start earlier"}
    assert telegram_bot.request_redo(db, 999, "c1", "x", operators=OPS) == ""


def test_expired_claim_can_be_taken_over_from_the_card(db):
    """After the TTL the row stays CLAIMED and the card still shows the holder's buttons, which nobody may
    use any more: a press has to say so and bring the Claim buttons back."""
    assert db.claim("c1", "manu", ttl_min=0.001)[0]
    time.sleep(0.1)
    assert db.state("c1") == "CLAIMED"
    for who in (11, 22):
        for action in ("approve", "reject", "release", "edit"):
            text, row = telegram_bot.handle_action(db, who, f"{action}:c1", operators=OPS)
            assert "expired" in text and row["state"] == "CLAIMED"
    assert [b[0] for b in telegram_bot.buttons_for(row, OPS)[0]] == ["Claim: Manu", "Claim: Samu"]
    assert "expired" in telegram_bot.card_text(row)
    text, row = telegram_bot.handle_action(db, 22, "claim:c1:samu", operators=OPS)
    assert text == "Claimed." and row["claimed_by"] == "samu"
    assert [b[0] for b in telegram_bot.buttons_for(row, OPS)[0]] == ["Approve", "Edit"]
    assert "expired" not in telegram_bot.card_text(row)
    assert telegram_bot.handle_action(db, 11, "approve:c1", operators=OPS)[0].startswith("Only")
    assert telegram_bot.handle_action(db, 22, "approve:c1", operators=OPS)[0] == "Approved."
    # stale buttons on a card that moved on answer with the state instead of "only the holder"
    assert telegram_bot.handle_action(db, 22, "reject:c1", operators=OPS)[0] == "Already approved."


def test_approve_and_publish_follow_clipwatcher_staging(db, tmp_path, monkeypatch):
    """Docker sets CLIPWATCHER_STAGING: Approve builds the approved package there, the publishers read it."""
    from clipwatcher.publish import youtube, youtube_browser

    monkeypatch.setattr(staging, "DEFAULT_ROOT", tmp_path / "repo" / "staging")
    root = tmp_path / "data" / "staging"
    monkeypatch.setenv("CLIPWATCHER_STAGING", str(root))
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.delenv("YOUTUBE_CREDENTIALS", raising=False)
    monkeypatch.delenv("YT_BROWSER_FORCE", raising=False)
    assert staging.staging_root() == root
    s, c = _videos(tmp_path)
    assert staging.stage_package("c1", s, c, {"streamer": "x"}, db=db) == root / "c1"
    telegram_bot.handle_action(db, 11, "claim:c1:manu", operators=OPS)
    assert telegram_bot.handle_action(db, 11, "approve:c1", operators=OPS)[0] == "Approved."
    assert (root / "approved" / "c1" / "short_9x16.mp4").read_bytes() == b"short"
    for pub in (youtube, youtube_browser):
        assert pub.publish_clip(db, "c1").startswith("dryrun-")
    staging.update_published("c1", "youtube", "https://y/1")
    assert (root / "published_clips.json").exists()
    assert not (tmp_path / "repo").exists()
    monkeypatch.setenv("CLIPWATCHER_STAGING", "")  # an empty value in .env means "not set"
    assert staging.staging_root() == tmp_path / "repo" / "staging"


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
