import pytest

from clipwatcher.publish import instagram, youtube
from clipwatcher.queue.db import ClipDB

META = {"streamer": "Gamer", "title": "T" * 90, "setup_line": "Sets up.", "game": "Elden Ring",
        "campaign_link": "https://c.example/x", "campaign_tags": ["#ad"]}  # fmt: skip


@pytest.fixture
def db(tmp_path):
    d = ClipDB(tmp_path / "p.db")
    d.add_clip("c1", "gamer")
    return d


def test_metadata():
    md = youtube.build_metadata(META)
    assert len(md["title"]) <= 60 and md["paid_promotion"]
    lines = md["description"].splitlines()
    assert lines[0].endswith("🎥 twitch.tv/gamer") and lines[1].endswith("👇")  # English 4-line copy
    assert "#ad" in md["description"] and "https://c.example/x" in md["description"]
    judged = youtube.build_metadata({**META, "description": "Wait...\nFollow for more!\n#Gamer #Shorts"})
    assert "Follow for more" not in judged["description"] and "twitch.tv/gamer" in judged["description"]
    assert not youtube.build_metadata({"streamer": "x"})["paid_promotion"]


def test_refuses_non_approved(db, tmp_path):
    for fn in (
        lambda: youtube.upload("c1", META, db, tmp_path / "v.mp4"),
        lambda: instagram.publish_reel("c1", META, db, "https://x"),
    ):
        with pytest.raises(youtube.NotApproved):
            fn()


def test_dry_run_no_network(db, tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.delenv("YOUTUBE_CREDENTIALS", raising=False)
    monkeypatch.setattr(youtube, "_service", lambda *_: pytest.fail("network"))
    db.claim("c1", "manu")
    db.approve("c1", "manu")
    assert youtube.upload("c1", META, db, tmp_path / "v.mp4").startswith("dryrun-")
    assert instagram.publish_reel("c1", META, db, "https://x").startswith("dryrun-")
    assert any(r["action"] == "youtube_dry_run" for r in db.audit_rows("c1"))


def test_publish_clip_from_approved_folder(db, tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.delenv("YOUTUBE_CREDENTIALS", raising=False)
    monkeypatch.setattr(youtube, "_service", lambda *_: pytest.fail("network"))
    folder = tmp_path / "approved" / "c1"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"streamer": "gamer"}))
    (folder / "short_9x16.mp4").write_bytes(b"x")
    db.claim("c1", "manu")
    db.approve("c1", "manu")
    assert youtube.publish_clip(db, "c1", tmp_path).startswith("dryrun-")
