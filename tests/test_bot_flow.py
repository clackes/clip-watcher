"""The approval bot's Telegram side against a fake bot: no network and no python-telegram-bot needed."""

import asyncio
import shutil
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipwatcher import config
from clipwatcher.queue import db as queue_db
from clipwatcher.queue import staging, telegram_bot
from clipwatcher.queue.db import ClipDB

CHAT = -1001
OPS = {"manu": {"telegram_id": 11, "label": "Manu"}, "samu": {"telegram_id": 22, "label": "Samu"}}


class FakeBot:
    def __init__(self):
        self.sent, self.edits, self.fail = [], [], 0

    def _send(self, kind, chat_id, text, kw):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("telegram is down")
        self.sent.append(SimpleNamespace(kind=kind, chat_id=chat_id, text=text, kw=kw))
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def send_message(self, chat_id, text, **kw):
        return self._send("text", chat_id, text, kw)

    async def send_video(self, chat_id, video, caption=None, **kw):
        return self._send("video", chat_id, caption, kw)

    def _edit(self, kind, chat_id, message_id, text, kw):
        self.edits.append(
            SimpleNamespace(kind=kind, chat_id=chat_id, message_id=message_id, text=text, kw=kw)
        )

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self._edit("text", chat_id, message_id, text, kw)

    async def edit_message_caption(self, chat_id=None, message_id=None, caption=None, **kw):
        self._edit("caption", chat_id, message_id, caption, kw)


class FakeApp:
    def __init__(self):
        self.bot, self.bot_data, self.tasks = FakeBot(), {}, []

    def create_task(self, coro, **kw):
        self.tasks.append(asyncio.ensure_future(coro))
        return self.tasks[-1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIPWATCHER_STAGING", raising=False)
    monkeypatch.delenv("CLIPWATCHER_YOUTUBE_MODE", raising=False)
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(telegram_bot, "_markup", lambda rows: rows)
    monkeypatch.setattr(telegram_bot, "_chat_id", lambda: CHAT)
    monkeypatch.setattr(telegram_bot, "video_attrs", lambda v: {"width": 1080, "height": 1920})
    monkeypatch.setattr(telegram_bot, "thumbnail", lambda v: None)
    monkeypatch.setattr(config, "operators", lambda: OPS)
    root = tmp_path / "staging"
    root.mkdir()
    db = ClipDB(root / "clips.db")
    short, clean = tmp_path / "s.mp4", tmp_path / "c.mp4"
    short.write_bytes(b"short")
    clean.write_bytes(b"clean")

    def stage(clip_id, **meta):
        staging.stage_package(clip_id, short, clean, {"streamer": "gamer", "title": "T", **meta}, root, db=db)
        return root / clip_id / "short_9x16.mp4"

    return SimpleNamespace(root=root, db=db, app=FakeApp(), stage=stage)


def later(db, seconds=600):
    """Move the queue's clocks back: leases have run out and the staging lane's grace period is over."""
    db._conn().execute(
        "UPDATE clips SET updated_at=updated_at-?, card_lease_until=card_lease_until-?", (seconds, seconds)
    )


def test_a_failed_card_is_posted_later_and_only_once(env):
    video = env.stage("c1")
    env.app.bot.fail = 1
    with pytest.raises(RuntimeError):
        asyncio.run(telegram_bot.post_card(env.app, env.db, "c1", video))
    assert env.db.get("c1")["card_message_id"] is None
    # the lane calling again and the bot's sweep both wait for the failed attempt's lease to run out
    assert asyncio.run(telegram_bot.post_card(env.app, env.db, "c1", video)) is False
    assert asyncio.run(telegram_bot.post_missing_cards(env.app, env.db, env.root)) == []
    later(env.db)
    assert asyncio.run(telegram_bot.post_missing_cards(env.app, env.db, env.root)) == ["c1"]
    (card,) = env.app.bot.sent
    assert card.kind == "video" and card.chat_id == CHAT and "Clip c1" in card.text
    assert [b[0] for b in card.kw["reply_markup"][0]] == ["Claim: Manu", "Claim: Samu"]
    row = env.db.get("c1")
    assert (row["card_chat_id"], row["card_message_id"], row["card_video"]) == (CHAT, 101, 1)
    # safe to call again: the clip keeps its one card
    later(env.db)
    assert asyncio.run(telegram_bot.post_card(env.app, env.db, "c1", video)) is False
    assert asyncio.run(telegram_bot.post_missing_cards(env.app, env.db, env.root)) == []
    assert len(env.app.bot.sent) == 1


def test_card_retries_are_bounded_and_end_without_the_video(env):
    env.stage("c1")
    env.app.bot.fail = 2
    for _ in range(2):
        later(env.db)
        assert asyncio.run(telegram_bot.post_missing_cards(env.app, env.db, env.root)) == []
    later(env.db)
    assert asyncio.run(telegram_bot.post_missing_cards(env.app, env.db, env.root)) == ["c1"]
    assert [m.kind for m in env.app.bot.sent] == ["text"] and env.db.get("c1")["card_video"] == 0

    env.stage("c2")
    env.app.bot.fail = 99
    for _ in range(5):
        later(env.db)
        asyncio.run(telegram_bot.post_missing_cards(env.app, env.db, env.root))
    assert env.app.bot.fail == 99 - queue_db.CARD_MAX_ATTEMPTS  # gave up: no card storm


def test_only_recent_ready_clips_without_a_card_get_one(env, monkeypatch):
    env.stage("fresh")  # its lane is still posting the card: left alone for CARD_GRACE_S
    assert asyncio.run(telegram_bot.post_missing_cards(env.app, env.db, env.root)) == []
    env.stage("old")
    env.stage("claimed")
    later(env.db)
    env.db._conn().execute("UPDATE clips SET created_at=? WHERE id='old'", (time.time() - 30 * 3600,))
    env.db.claim("claimed", "manu")
    assert asyncio.run(telegram_bot.post_missing_cards(env.app, env.db, env.root)) == ["fresh"]
    monkeypatch.setattr(config, "global_config", lambda: {"publish": {"card_max_age_h": 0}})
    env.stage("vod")
    later(env.db)
    assert asyncio.run(telegram_bot.post_missing_cards(env.app, env.db, env.root)) == []


def test_clips_queued_before_cards_were_tracked_are_not_posted_again(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(queue_db.SCHEMA)  # a file from before the card columns
    con.execute(
        "INSERT INTO clips(id,streamer,state,created_at,updated_at) VALUES('legacy','x','READY',?,?)",
        (time.time() - 3600, time.time() - 3600),
    )
    con.commit()
    con.close()
    db = ClipDB(path)
    assert db.get("legacy")["card_message_id"] == queue_db.LEGACY_CARD
    db.add_clip("new", "x")
    assert ClipDB(path).get("new")["card_message_id"] is None  # opening the file again marks nothing
    assert [r["id"] for r in db.cardless(grace_s=0)] == ["new"]


def test_card_of_an_expired_claim_goes_back_to_the_claim_buttons(env):
    asyncio.run(telegram_bot.post_card(env.app, env.db, "c1", env.stage("c1")))
    asyncio.run(telegram_bot.post_card(env.app, env.db, "c2", env.stage("c2")))
    assert env.db.claim("c1", "manu", ttl_min=0.001)[0] and env.db.claim("c2", "samu")[0]
    time.sleep(0.1)
    asyncio.run(telegram_bot.sweep(env.app, env.db, env.root))
    row = env.db.get("c1")
    assert (row["state"], row["claimed_by"]) == ("READY", None) and env.db.state("c2") == "CLAIMED"
    (edit,) = env.app.bot.edits  # only the expired one, on the card the queue recorded
    assert (edit.kind, edit.chat_id, edit.message_id) == ("caption", CHAT, 101)
    assert [b[0] for b in edit.kw["reply_markup"][0]] == ["Claim: Manu", "Claim: Samu"]
    assert "State: READY" in edit.text and "manu" not in edit.text
    assert "claim_expired" in [r["action"] for r in env.db.audit_rows("c1")]
    assert not env.db.approve("c1", "manu")  # the old holder lost the lock
    asyncio.run(telegram_bot.sweep(env.app, env.db, env.root))
    assert len(env.app.bot.edits) == 1 and len(env.app.bot.sent) == 2


def test_housekeeping_keeps_sweeping_after_a_failed_pass(env, monkeypatch):
    env.stage("c1")
    later(env.db)
    real, calls = telegram_bot.sweep, []

    async def flaky(app, db, root=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        await real(app, db, root)

    monkeypatch.setattr(telegram_bot, "sweep", flaky)

    async def run():
        task = asyncio.create_task(telegram_bot.housekeeping(env.app, env.db, env.root, every_s=0.01))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert len(calls) >= 2 and len(env.app.bot.sent) == 1


# --------------------------------------------------------------------------- buttons and uploads
class FakeMessage:
    """The card a button sits on (a video card: its text is the caption)."""

    def __init__(self):
        self.text, self.chat, self.message_id, self.replies = None, SimpleNamespace(id=CHAT), 555, []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeQuery:
    """A button press. broken: Telegram refuses the answer and the card edit (query too old, timeout)."""

    def __init__(self, user_id, data, broken=False):
        self.from_user, self.data, self.message = SimpleNamespace(id=user_id), data, FakeMessage()
        self.broken, self.answers, self.cards = broken, [], []

    async def answer(self, text):
        if self.broken:
            raise RuntimeError("Query is too old")
        self.answers.append(text)

    async def edit_message_caption(self, caption, reply_markup=None):
        if self.broken:
            raise RuntimeError("Timed out")
        self.cards.append(SimpleNamespace(text=caption, buttons=reply_markup))


class FakeStudio:
    """Stands in for the Chrome / YouTube Studio session behind publish/youtube_browser.py."""

    def __init__(self):
        self.uploads, self.error, self.busy, self.overlapped = [], None, 0, False

    async def __call__(self, video, title, description, *, private, on_video_id=None, **kw):
        self.busy += 1
        self.overlapped = self.overlapped or self.busy > 1
        try:
            await asyncio.sleep(0.02)
            self.uploads.append(Path(video).parent.name)
            if self.error:
                raise self.error
            video_id = f"video{len(self.uploads):06d}"
            on_video_id(video_id)
            return f"https://youtube.com/shorts/{video_id}"
        finally:
            self.busy -= 1


@pytest.fixture
def studio(env, monkeypatch):
    from clipwatcher.publish import youtube_browser

    fake = FakeStudio()
    monkeypatch.setattr(youtube_browser, "_upload", fake)
    monkeypatch.setenv("YT_BROWSER_FORCE", "1")  # go through the (fake) browser although DRY_RUN=1: private
    monkeypatch.setattr(config, "global_config", lambda: {"publish": {"youtube_mode": "browser"}})
    return fake


async def press(env, user_id, data, **kw):
    """Press a button and wait for the upload it may have started."""
    q = FakeQuery(user_id, data, **kw)
    await telegram_bot.press(env.app, env.db, q, root=env.root)
    await asyncio.gather(*env.app.tasks)
    return q


def approve(env, *clip_ids):
    for cid in clip_ids:
        assert env.db.claim(cid, "manu")[0] and env.db.approve(cid, "manu")
        staging.mark_approved(cid, "manu", env.root)


def test_upload_starts_even_when_telegram_fails_on_the_card(env, studio):
    env.stage("c1")

    async def go():
        await press(env, 11, "claim:c1:manu")
        return await press(env, 11, "approve:c1", broken=True)

    q = asyncio.run(go())
    assert q.answers == [] and q.cards == []  # Telegram took neither the answer nor the redrawn card
    assert studio.uploads == ["c1"]
    row = env.db.get("c1")
    assert (row["state"], row["youtube_id"]) == ("PUBLISHED", "video000001")
    assert q.message.replies == ["YouTube private (DRY_RUN=1): https://youtube.com/shorts/video000001"]


def test_failed_upload_shows_on_the_card_and_any_operator_can_retry(env, studio):
    env.stage("c1")
    studio.error = RuntimeError("YouTube profile is not signed in")

    async def go():
        await press(env, 11, "claim:c1:manu")
        q = await press(env, 11, "approve:c1")
        card = q.cards[-1]  # drawn after the failure: the press itself must not paint over it
        assert "State: APPROVED" in card.text
        assert "YouTube upload failed: RuntimeError: YouTube profile is not signed in" in card.text
        assert card.buttons == [[("Retry YouTube upload", "publish:c1")]]
        assert q.message.replies[-1].startswith("YouTube upload failed for c1: YouTube profile is not")
        assert env.db.state("c1") == "APPROVED"
        assert (await press(env, 999, "publish:c1")).answers == []  # not an operator: ignored

        studio.error = None
        q = await press(env, 22, "publish:c1")  # the other operator retries
        assert q.answers == ["Uploading to YouTube."]
        card = q.cards[-1]
        assert "State: PUBLISHED" in card.text and "upload failed" not in card.text
        assert "YouTube: https://youtube.com/shorts/video000002" in card.text and card.buttons == []
        assert q.message.replies == ["YouTube private (DRY_RUN=1): https://youtube.com/shorts/video000002"]
        q = await press(env, 22, "publish:c1")  # a Retry button left on an old copy of the card
        assert q.answers == ["Nothing to upload: the clip is published."]

    asyncio.run(go())
    assert studio.uploads == ["c1", "c1"]
    actions = [r["action"] for r in env.db.audit_rows("c1")]
    assert "youtube_failed" in actions and "publish_retry" in actions


def test_retry_builds_the_approved_package_when_the_approval_could_not(env, studio):
    env.stage("c1")

    async def go():
        await press(env, 11, "claim:c1:manu")
        shutil.move(env.root / "c1", env.root / "c1.away")  # the staged package is unreachable at Approve
        q = await press(env, 11, "approve:c1")
        assert q.answers == ["Approved."] and "FileNotFoundError" in q.cards[-1].text
        assert q.cards[-1].buttons == [[("Retry YouTube upload", "publish:c1")]] and studio.uploads == []
        shutil.move(env.root / "c1.away", env.root / "c1")
        q = await press(env, 22, "publish:c1")
        assert "State: PUBLISHED" in q.cards[-1].text

    asyncio.run(go())
    assert studio.uploads == ["c1"] and (env.root / "approved" / "c1" / "clean_16x9.mp4").exists()


def test_uploads_run_one_at_a_time_and_never_twice_for_one_clip(env, studio):
    env.stage("a")
    env.stage("b")
    approve(env, "a", "b")

    async def go():
        await asyncio.gather(
            telegram_bot.publish(env.app, env.db, "a", root=env.root),
            telegram_bot.publish(env.app, env.db, "a", root=env.root),  # Retry pressed twice
            telegram_bot.publish(env.app, env.db, "b", root=env.root),
        )

    asyncio.run(go())
    assert sorted(studio.uploads) == ["a", "b"] and not studio.overlapped  # one Chrome profile


def test_approved_clips_are_uploaded_after_a_restart(env, studio, monkeypatch):
    clips = ("never", "failed", "dry", "half", "old", "ready")
    for cid in clips:
        asyncio.run(telegram_bot.post_card(env.app, env.db, cid, env.stage(cid)))
    approve(env, *clips[:-1])
    env.db.publish_failed("failed", "ConnectionError: network down")
    env.db.log("publisher", "youtube_browser_dry_run", "dry", "would upload short_9x16.mp4")
    env.db.note_upload("half", "aBcDeFgHiJk")  # Studio had the file when the bot went down
    env.db._conn().execute("UPDATE clips SET updated_at=? WHERE id='old'", (time.time() - 30 * 3600,))

    monkeypatch.setattr(config, "global_config", lambda: {"publish": {"youtube_on_approve": False}})
    assert asyncio.run(telegram_bot.resume_publishing(env.app, env.db, env.root)) == []
    assert studio.uploads == [] and env.app.bot.edits == []

    monkeypatch.setattr(config, "global_config", lambda: {"publish": {}})
    assert asyncio.run(telegram_bot.resume_publishing(env.app, env.db, env.root)) == ["never", "failed"]
    assert studio.uploads == ["never", "failed"] and not studio.overlapped
    states = {cid: env.db.state(cid) for cid in clips}
    assert states == {"never": "PUBLISHED", "failed": "PUBLISHED", "dry": "APPROVED", "half": "APPROVED",
                      "old": "APPROVED", "ready": "READY"}  # fmt: skip
    cards = {e.message_id: e for e in env.app.bot.edits}  # 101.. in the order the cards were posted
    assert "State: PUBLISHED" in cards[101].text and "youtube.com/shorts/video000001" in cards[101].text
    assert cards[101].kw["reply_markup"] == [] and set(cards) == {101, 102, 104}
    # the one that may already be on YouTube is left to an operator
    assert "https://youtube.com/shorts/aBcDeFgHiJk: check YouTube Studio" in cards[104].text
    assert cards[104].kw["reply_markup"] == [[("Retry YouTube upload", "publish:half")]]
    told = [m.text for m in env.app.bot.sent if m.kind == "text"]  # no pressed card to reply under
    assert told == [f"YouTube private (DRY_RUN=1): https://youtube.com/shorts/video00000{n}" for n in (1, 2)]
    assert asyncio.run(telegram_bot.resume_publishing(env.app, env.db, env.root)) == []
    assert len(studio.uploads) == 2
