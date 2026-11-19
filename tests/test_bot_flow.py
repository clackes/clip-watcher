"""The approval bot's Telegram side against a fake bot: no network and no python-telegram-bot needed."""

import asyncio
import sqlite3
import time
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


class FakeApp:
    def __init__(self):
        self.bot, self.bot_data, self.tasks = FakeBot(), {}, []

    def create_task(self, coro, **kw):
        self.tasks.append(asyncio.ensure_future(coro))
        return self.tasks[-1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIPWATCHER_STAGING", raising=False)
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
