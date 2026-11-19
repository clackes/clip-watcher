"""SQLite clip queue: clips, jobs, audit. Claim lock is one atomic UPDATE."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .. import config

READY, CLAIMED, APPROVED, SCHEDULED, PUBLISHED, REJECTED = (
    "READY", "CLAIMED", "APPROVED", "SCHEDULED", "PUBLISHED", "REJECTED",
)  # fmt: skip

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips(
  id TEXT PRIMARY KEY, streamer TEXT, state TEXT NOT NULL, claimed_by TEXT, claim_until REAL,
  approved_by TEXT, meta_json TEXT, created_at REAL, updated_at REAL, youtube_id TEXT,
  instagram_id TEXT, score REAL
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
  attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL, payload TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, clip_id TEXT, detail TEXT
);
"""

# Columns added to clips after the first release; _migrate() adds them to an existing file.
COLUMNS = {
    # the clip's approval card in Telegram: where it is, whether it carries the video, and the lease of
    # whoever is posting it right now (the live lane or the approval bot)
    "card_chat_id": "INTEGER",
    "card_message_id": "INTEGER",
    "card_video": "INTEGER",
    "card_lease_until": "REAL",
    "card_attempts": "INTEGER",
}
LEGACY_CARD = 0  # card_message_id of clips queued before cards were tracked: taken as posted, never re-posted
CARD_MAX_ATTEMPTS = 3


class ClipDB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        c = self._conn()
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        self._migrate(c)

    def _migrate(self, c: sqlite3.Connection) -> None:
        """Add COLUMNS to a file made by an older version (CREATE TABLE IF NOT EXISTS leaves it alone)."""
        have = {r["name"] for r in c.execute("PRAGMA table_info(clips)")}
        if all(name in have for name in COLUMNS):
            return
        c.execute("BEGIN IMMEDIATE")  # the lane and the bot may open the same file at the same moment
        try:
            have = {r["name"] for r in c.execute("PRAGMA table_info(clips)")}
            for name, kind in COLUMNS.items():
                if name in have:
                    continue
                c.execute(f"ALTER TABLE clips ADD COLUMN {name} {kind}")
                if name == "card_message_id":
                    c.execute("UPDATE clips SET card_message_id=?", (LEGACY_CARD,))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
        return c

    # -- audit -------------------------------------------------------------
    def log(self, actor: str, action: str, clip_id: str | None, detail: str = "") -> None:
        self._conn().execute(
            "INSERT INTO audit(ts,actor,action,clip_id,detail) VALUES(?,?,?,?,?)",
            (time.time(), actor, action, clip_id, detail),
        )

    def audit_rows(self, clip_id: str | None = None) -> list[sqlite3.Row]:
        q, a = "SELECT * FROM audit", ()
        if clip_id:
            q, a = q + " WHERE clip_id=?", (clip_id,)
        return self._conn().execute(q + " ORDER BY id", a).fetchall()

    # -- clips -------------------------------------------------------------
    def add_clip(
        self, clip_id: str, streamer: str, meta: dict[str, Any] | None = None, score: float = 0.0
    ) -> bool:
        """Queue a staged clip as READY. Staging the same id again refreshes meta and score only while the
        clip is still READY; a claimed, approved, rejected or published clip keeps its state, operator and
        video ids (returns False). One atomic statement, like the claim."""
        now = time.time()
        known = self.get(clip_id) is not None  # only picks the audit label
        cur = self._conn().execute(
            "INSERT INTO clips(id,streamer,state,meta_json,created_at,updated_at,score)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET streamer=excluded.streamer, meta_json=excluded.meta_json,"
            " score=excluded.score, updated_at=excluded.updated_at WHERE clips.state='READY'",
            (clip_id, streamer, READY, json.dumps(meta or {}), now, now, score),
        )
        if cur.rowcount != 1:
            self.log("system", "restage_refused", clip_id, f"state={self.state(clip_id)}")
            return False
        self.log("system", "restage" if known else "add", clip_id, f"score={score}")
        return True

    def get(self, clip_id: str) -> dict[str, Any] | None:
        r = self._conn().execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        return dict(r) if r else None

    def state(self, clip_id: str) -> str | None:
        r = self.get(clip_id)
        return r["state"] if r else None

    def list_clips(self, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            rows = self._conn().execute("SELECT * FROM clips WHERE state=? ORDER BY created_at", (state,))
        else:
            rows = self._conn().execute("SELECT * FROM clips ORDER BY created_at")
        return [dict(r) for r in rows.fetchall()]

    def claim(self, clip_id: str, operator: str, ttl_min: float | None = None) -> tuple[bool, str | None]:
        """Atomic claim. Returns (ok, holder): holder is the current owner of the lock."""
        if ttl_min is None:
            ttl_min = config.global_config().get("publish", {}).get("claim_ttl_min", 15)
        now = time.time()
        cur = self._conn().execute(
            "UPDATE clips SET state='CLAIMED', claimed_by=?, claim_until=?, updated_at=? "
            "WHERE id=? AND (state='READY' OR (state='CLAIMED' AND claim_until<?))",
            (operator, now + ttl_min * 60, now, clip_id, now),
        )
        if cur.rowcount == 1:
            self.log(operator, "claim", clip_id)
            return True, operator
        row = self.get(clip_id)
        holder = row["claimed_by"] if row and row["state"] == CLAIMED else None
        self.log(operator, "claim_denied", clip_id, f"holder={holder}")
        return False, holder

    def _holder_update(self, clip_id: str, operator: str, set_sql: str, args: tuple) -> bool:
        now = time.time()
        cur = self._conn().execute(
            f"UPDATE clips SET {set_sql}, updated_at=? "
            "WHERE id=? AND state='CLAIMED' AND claimed_by=? AND claim_until>=?",
            (*args, now, clip_id, operator, now),
        )
        return cur.rowcount == 1

    def approve(self, clip_id: str, operator: str) -> bool:
        ok = self._holder_update(clip_id, operator, "state='APPROVED', approved_by=?", (operator,))
        self.log(operator, "approve" if ok else "approve_denied", clip_id)
        return ok

    def reject(self, clip_id: str, operator: str, reason: str = "") -> bool:
        ok = self._holder_update(clip_id, operator, "state='REJECTED'", ())
        self.log(operator, "reject" if ok else "reject_denied", clip_id, reason)
        return ok

    def release(self, clip_id: str, operator: str) -> bool:
        ok = self._holder_update(clip_id, operator, "state='READY', claimed_by=NULL, claim_until=NULL", ())
        self.log(operator, "release" if ok else "release_denied", clip_id)
        return ok

    def edit(self, clip_id: str, operator: str, changes: dict[str, Any]) -> bool:
        row = self.get(clip_id)
        if not row:
            return False
        meta = json.loads(row["meta_json"] or "{}")
        meta.update(changes)
        ok = self._holder_update(clip_id, operator, "meta_json=?", (json.dumps(meta),))
        self.log(operator, "edit" if ok else "edit_denied", clip_id, json.dumps(changes)[:500])
        return ok

    def auto_approve(self, min_score: float | None = None) -> list[str]:
        """Approve READY clips scoring >= min_score, only if publish.auto_approve is on."""
        pub = config.global_config().get("publish", {})
        if not pub.get("auto_approve", False):
            return []
        if min_score is None:
            min_score = pub.get("auto_approve_min", 8)
        now = time.time()
        rows = (
            self._conn()
            .execute("SELECT id FROM clips WHERE state='READY' AND score>=?", (min_score,))
            .fetchall()
        )
        done = []
        for r in rows:
            cur = self._conn().execute(
                "UPDATE clips SET state='APPROVED', approved_by='auto', updated_at=? "
                "WHERE id=? AND state='READY'",
                (now, r["id"]),
            )
            if cur.rowcount == 1:
                self.log("auto", "auto_approve", r["id"], f"min={min_score}")
                done.append(r["id"])
        return done

    def set_state(self, clip_id: str, state: str, actor: str = "system", **fields: Any) -> None:
        sets = ["state=?", "updated_at=?"]
        args: list[Any] = [state, time.time()]
        for k, v in fields.items():
            if k not in ("youtube_id", "instagram_id", "approved_by"):
                raise ValueError(k)
            sets.append(f"{k}=?")
            args.append(v)
        self._conn().execute(f"UPDATE clips SET {', '.join(sets)} WHERE id=?", (*args, clip_id))
        self.log(actor, f"state:{state}", clip_id)

    # -- approval cards ----------------------------------------------------
    def reserve_card(self, clip_id: str, lease_s: float = 300) -> int:
        """Atomically take the right to post a clip's card. Returns the attempt number (1, 2, ...), or 0 when
        the clip has a card or someone holds the lease (is posting it, or failed less than lease_s ago)."""
        now = time.time()
        cur = self._conn().execute(
            "UPDATE clips SET card_lease_until=?, card_attempts=COALESCE(card_attempts,0)+1 "
            "WHERE id=? AND card_message_id IS NULL AND (card_lease_until IS NULL OR card_lease_until<?)",
            (now + lease_s, clip_id, now),
        )
        if cur.rowcount != 1:
            return 0
        return int((self.get(clip_id) or {}).get("card_attempts") or 1)

    def set_card(self, clip_id: str, chat_id: int, message_id: int, video: bool = False) -> None:
        self._conn().execute(
            "UPDATE clips SET card_chat_id=?, card_message_id=?, card_video=?, card_lease_until=NULL "
            "WHERE id=?",
            (chat_id, message_id, int(video), clip_id),
        )
        self.log("system", "card", clip_id, f"chat={chat_id} message={message_id}")

    def cardless(self, grace_s: float = 120, max_age_s: float = 24 * 3600) -> list[dict[str, Any]]:
        """READY clips no operator can see: no card, nobody posting one, fewer than CARD_MAX_ATTEMPTS tries.
        Younger than grace_s is left to the lane that staged it; older than max_age_s is left alone."""
        now = time.time()
        rows = self._conn().execute(
            "SELECT * FROM clips WHERE state='READY' AND card_message_id IS NULL "
            "AND (card_lease_until IS NULL OR card_lease_until<?) AND COALESCE(card_attempts,0)<? "
            "AND updated_at<=? AND created_at>=? ORDER BY created_at",
            (now, CARD_MAX_ATTEMPTS, now - grace_s, now - max_age_s),
        )
        return [dict(r) for r in rows.fetchall()]

    # -- jobs --------------------------------------------------------------
    def enqueue_job(self, stage: str, payload: dict[str, Any] | None = None) -> int:
        cur = self._conn().execute(
            "INSERT INTO jobs(stage,state,payload) VALUES(?,?,?)",
            (stage, "PENDING", json.dumps(payload or {})),
        )
        return int(cur.lastrowid)

    def claim_job(self, stage: str, lease_s: float = 300) -> dict[str, Any] | None:
        """Lease the oldest PENDING job (or RUNNING with an expired lease). Atomic."""
        c = self._conn()
        now = time.time()
        c.execute("BEGIN IMMEDIATE")
        try:
            r = c.execute(
                "SELECT id FROM jobs WHERE stage=? "
                "AND (state='PENDING' OR (state='RUNNING' AND lease_until<?)) "
                "ORDER BY id LIMIT 1",
                (stage, now),
            ).fetchone()
            if not r:
                c.execute("COMMIT")
                return None
            c.execute(
                "UPDATE jobs SET state='RUNNING', attempts=attempts+1, lease_until=? WHERE id=?",
                (now + lease_s, r["id"]),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        row = dict(c.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone())
        row["payload"] = json.loads(row["payload"] or "{}")
        return row

    def finish_job(self, job_id: int, error: str | None = None) -> None:
        self._conn().execute(
            "UPDATE jobs SET state=?, error=?, lease_until=NULL WHERE id=?",
            ("FAILED" if error else "DONE", error, job_id),
        )
