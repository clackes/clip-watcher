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


class FakeYouTube:
    """Stands in for the Data API client: insert() hands out one video id per call."""

    def __init__(self, insert_error=None, list_error=None, privacy="private"):
        self.insert_error, self.list_error = insert_error, list_error
        self.privacy, self.inserted = privacy, []

    def videos(self):
        return self

    def insert(self, **kw):
        self.inserted.append(kw["body"])
        return self

    def next_chunk(self):
        if self.insert_error:
            raise self.insert_error
        return None, {"id": f"VID{len(self.inserted)}"}

    def list(self, **kw):
        return self

    def execute(self):
        if self.list_error:
            raise self.list_error
        return {"items": [{"status": {"privacyStatus": self.privacy}}]}


@pytest.fixture
def approved(db, tmp_path, monkeypatch):
    """c1 approved with its package in tmp_path/approved; credentials 'set', DRY_RUN=1 (uploads private)."""
    import json

    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("YOUTUBE_CREDENTIALS", "token.json")
    monkeypatch.setattr(youtube, "_media", lambda video: str(video))
    folder = tmp_path / "approved" / "c1"
    folder.mkdir(parents=True)
    (folder / "meta.json").write_text(json.dumps({"streamer": "gamer", "title": "T"}))
    (folder / "short_9x16.mp4").write_bytes(b"x")
    db.claim("c1", "manu")
    db.approve("c1", "manu")
    return db


def _audit(db, action):
    return [r["detail"] for r in db.audit_rows("c1") if r["action"] == action]


def test_api_upload_is_recorded_even_when_the_check_after_it_fails(approved, tmp_path, monkeypatch):
    """The video is on YouTube once insert returns: an error after that must not leave the clip APPROVED,
    or the retry uploads it a second time."""
    yt = FakeYouTube(list_error=TimeoutError("status check timed out"))
    monkeypatch.setattr(youtube, "_service", lambda creds: yt)
    assert youtube.publish_clip(approved, "c1", tmp_path) == "https://youtube.com/shorts/VID1"
    row = approved.get("c1")
    assert (row["state"], row["youtube_id"]) == ("PUBLISHED", "VID1")
    assert _audit(approved, "state:PUBLISHED") == ["youtube_id=VID1"]  # the id is in the audit log
    assert "status check timed out" in _audit(approved, "youtube_privacy_unchecked")[0]
    with pytest.raises(youtube.NotApproved):
        youtube.publish_clip(approved, "c1", tmp_path)
    assert len(yt.inserted) == 1 and yt.inserted[0]["status"]["privacyStatus"] == "private"


def test_failed_upload_is_audited_and_can_be_retried(approved, tmp_path, monkeypatch):
    yt = FakeYouTube(insert_error=ConnectionError("quota exceeded"))
    monkeypatch.setattr(youtube, "_service", lambda creds: yt)
    with pytest.raises(ConnectionError):
        youtube.publish_clip(approved, "c1", tmp_path)
    row = approved.get("c1")
    assert row["state"] == "APPROVED" and row["youtube_id"] is None
    assert row["publish_error"] == "ConnectionError: quota exceeded"
    assert _audit(approved, "youtube_failed") == ["ConnectionError: quota exceeded"]
    yt.insert_error = None
    assert youtube.publish_clip(approved, "c1", tmp_path).endswith("/VID2")
    row = approved.get("c1")
    assert (row["state"], row["youtube_id"], row["publish_error"]) == ("PUBLISHED", "VID2", None)


def test_browser_upload_keeps_the_id_of_a_video_that_did_not_finish(approved, tmp_path, monkeypatch):
    from clipwatcher.publish import youtube_browser

    monkeypatch.setenv("YT_BROWSER_FORCE", "1")  # upload (private) although DRY_RUN=1; the browser is faked
    seen = {}

    async def stuck(video, title, description, *, private, on_video_id=None, **kw):
        seen.update(video=video, private=private)
        on_video_id("aBcDeFgHiJk")  # Studio took the file ...
        raise TimeoutError("Next button never appeared")  # ... and the dialog was never finished

    monkeypatch.setattr(youtube_browser, "_upload", stuck)
    with pytest.raises(TimeoutError):
        youtube_browser.publish_clip(approved, "c1", tmp_path)
    row = approved.get("c1")
    assert (row["state"], row["youtube_id"]) == ("APPROVED", "aBcDeFgHiJk") and seen["private"] is True
    assert _audit(approved, "youtube_video_id") == ["aBcDeFgHiJk"]
    assert _audit(approved, "youtube_failed") == ["TimeoutError: Next button never appeared"]

    async def done(video, title, description, *, private, on_video_id=None, **kw):
        on_video_id("dQw4w9WgXcQ")
        return "https://youtube.com/shorts/dQw4w9WgXcQ"

    monkeypatch.setattr(youtube_browser, "_upload", done)
    assert youtube_browser.publish_clip(approved, "c1", tmp_path) == "https://youtube.com/shorts/dQw4w9WgXcQ"
    row = approved.get("c1")
    assert (row["state"], row["youtube_id"], row["publish_error"]) == ("PUBLISHED", "dQw4w9WgXcQ", None)
    assert _audit(approved, "state:PUBLISHED") == ["youtube_id=dQw4w9WgXcQ"]
