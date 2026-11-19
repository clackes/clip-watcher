"""Telegram approval queue. Button logic is pure; python-telegram-bot is imported lazily."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .. import config
from . import staging
from .db import ClipDB

log = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 50 * 1024 * 1024


def operator_for(telegram_id: int, operators: dict[str, Any] | None = None) -> str | None:
    """Operator name for a Telegram user id, or None (ignored)."""
    ops = config.operators() if operators is None else operators
    for name, o in ops.items():
        tid = o.get("telegram_id")
        if tid and int(tid) == int(telegram_id):
            return name
    return None


STATE_LINE = {
    "READY": "🟡 Waiting for someone to check it",
    "CLAIMED": "👀 {who} is checking it",
    "APPROVED": "✅ Approved, uploading to YouTube",
    "SCHEDULED": "🕒 Scheduled",
    "PUBLISHED": "📺 Live on YouTube",
    "REJECTED": "🗑 Discarded",
}
FLAG_TEXT = {"sp": "story spoiler", "mu": "music (may get a copyright claim)", "gb": "gambling"}
APPROVED = "✅ Approved! Uploading to YouTube…"
REDO_ASK = "🔁 Redo: what should change?"


def card_text(row: dict[str, Any]) -> str:
    """Short card: who, how good, what it is, why. Plain words an operator gets at a glance."""
    import json

    meta = json.loads(row.get("meta_json") or "{}")
    score = meta.get("judge_score") or meta.get("score")
    head = f"🎬 {row['streamer']}"
    if isinstance(score, (int, float)) and score <= 10:
        head += f"  ·  ⭐ {round(score)}/10"
    lines = [head]
    if meta.get("title"):
        lines.append(f"📌 {meta['title']}")
    if meta.get("why"):
        lines.append(f"💬 {meta['why']}")
    flagged = [FLAG_TEXT.get(k, k) for k, v in (meta.get("flags") or {}).items() if v]
    if flagged:
        lines.append("⚠️ Careful: " + ", ".join(flagged))
    if meta.get("redo_note"):
        lines.append(f"🔁 Redone: {meta['redo_note']}")
    state = STATE_LINE.get(row["state"], row["state"]).format(who=row.get("claimed_by") or "someone")
    lines.append(state)
    if meta.get("description"):
        lines.append("\n📝 Description:\n" + str(meta["description"]))
    lines.append(f"\n🆔 {row['id']}")
    return "\n".join(lines)


def buttons_for(row: dict[str, Any], operators: dict[str, Any] | None = None) -> list[list[tuple[str, str]]]:
    """Rows of (label, callback_data). READY: one "I'll check it" button per operator, and Redo.
    CLAIMED: the holder's actions."""
    cid = row["id"]
    if row["state"] == "READY":
        ops = config.operators() if operators is None else operators
        return [
            [(f"🙋 {o.get('label', n)}", f"claim:{cid}:{n}") for n, o in ops.items()],
            [("🔁 Redo", f"redo:{cid}")],
        ]
    if row["state"] == "CLAIMED":
        return [
            [("✅ Publish", f"approve:{cid}"), ("✏️ Change title", f"edit:{cid}")],
            [("🔁 Redo", f"redo:{cid}"), ("🗑 Discard", f"reject:{cid}")],
            [("↩️ Not mine", f"release:{cid}")],
        ]
    return []


def handle_action(
    db: ClipDB,
    telegram_id: int,
    data: str,
    *,
    operators: dict[str, Any] | None = None,
    root: Path | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Process a button press. Returns (answer text, updated clip row or None). Pure DB logic."""
    op = operator_for(telegram_id, operators)
    if op is None:
        return "", None  # ignore non-operators silently
    parts = data.split(":")
    action, clip_id = parts[0], parts[1] if len(parts) > 1 else ""
    if action == "claim":
        who = parts[2] if len(parts) > 2 else op
        if who != op:
            return "✋ That button is for someone else.", db.get(clip_id)
        ok, holder = db.claim(clip_id, op)
        return ("👍 It's yours, check it out." if ok else f"✋ {holder} is already on it."), db.get(clip_id)
    if action == "approve":
        ok = db.approve(clip_id, op)
        if ok:
            try:
                staging.mark_approved(clip_id, op, root)
            except FileNotFoundError:
                log.warning("staging folder missing for %s", clip_id)
        return (APPROVED if ok else "✋ Only the person checking it can publish."), db.get(clip_id)
    if action == "reject":
        ok = db.reject(clip_id, op)
        return ("🗑 Discarded." if ok else "✋ Only the person checking it can discard."), db.get(clip_id)
    if action == "release":
        ok = db.release(clip_id, op)
        return ("↩️ Back in the queue." if ok else "✋ It isn't yours."), db.get(clip_id)
    if action == "edit":
        row = db.get(clip_id)
        if not row or row["claimed_by"] != op:
            return "✋ Only the person checking it can change the title.", row
        return "✏️ Send the new title.", row
    if action == "redo":
        row = db.get(clip_id)
        if not row or row["state"] not in ("READY", "CLAIMED"):
            return "✋ This clip can't be redone any more.", row
        return REDO_ASK, row
    if action == "redogo":  # Redo without a note
        return request_redo(db, telegram_id, clip_id, "", operators), db.get(clip_id)
    return "🤷 Unknown button.", db.get(clip_id)


def request_redo(
    db: ClipDB, telegram_id: int, clip_id: str, note: str, operators: dict[str, Any] | None = None
) -> str:
    """Queue a redo for the streamer's live lane (it re-cuts, re-judges and re-renders with the note)."""
    op = operator_for(telegram_id, operators)
    row = db.get(clip_id)
    if op is None or not row:
        return ""
    note = note.strip()[:300]
    db.enqueue_job(f"redo:{row['streamer']}", {"clip_id": clip_id, "operator": op, "note": note})
    db.log(op, "redo", clip_id, note)
    return "🔁 Got it, redoing it. The new version arrives here in a few minutes."


def handle_edit_reply(
    db: ClipDB, telegram_id: int, clip_id: str, text: str, operators: dict[str, Any] | None = None
) -> bool:
    op = operator_for(telegram_id, operators)
    if op is None:
        return False
    return db.edit(clip_id, op, {"title": text.strip()[:60]})


def build_application(db: ClipDB, token: str | None = None) -> Any:
    """Create the python-telegram-bot Application (lazy import)."""
    from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

    token = token or os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    def markup(row: dict[str, Any]) -> Any:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=cb) for label, cb in r] for r in buttons_for(row)]
        )

    async def on_button(update: Any, context: Any) -> None:
        q = update.callback_query
        text, row = handle_action(db, q.from_user.id, q.data)
        if not text:
            return
        await q.answer(text)
        if row is None:
            return
        if q.data.startswith("redogo:"):  # pressed under the redo question, not on the card
            context.user_data.pop("redo", None)
            await q.edit_message_text(text)
            return
        from telegram.error import BadRequest

        try:
            # Video cards carry the text as a caption; text-only cards as message text.
            if q.message is not None and q.message.text is None:
                await q.edit_message_caption(card_text(row), reply_markup=markup(row))
            else:
                await q.edit_message_text(card_text(row), reply_markup=markup(row))
        except BadRequest as e:
            if "not modified" not in str(e):  # pressing a button that changes nothing is fine
                raise
        if q.data.startswith("approve:") and text == APPROVED and _youtube_on_approve():
            context.application.create_task(_publish(q.message, db, row["id"]))
        if q.data.startswith("edit:") and text.startswith("✏️"):
            await q.message.reply_text(f"✏️ New title for {row['id']}:", reply_markup=ForceReply())
            context.user_data["editing"] = row["id"]
        if q.data.startswith("redo:") and text == REDO_ASK:
            skip = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔁 Just redo it", callback_data=f"redogo:{row['id']}")]]
            )
            await q.message.reply_text(
                '🔁 Write what to change (e.g. "start earlier", "end on the laugh", "focus on the crash"), '
                "or tap the button to redo it as it is.",
                reply_markup=skip,
            )
            context.user_data["redo"] = row["id"]

    async def on_text(update: Any, context: Any) -> None:
        cid = context.user_data.pop("editing", None)
        if cid and handle_edit_reply(db, update.effective_user.id, cid, update.message.text):
            await update.message.reply_text("✏️ Title changed.")
            return
        rid = context.user_data.pop("redo", None)
        if rid:
            msg = request_redo(db, update.effective_user.id, rid, update.message.text)
            if msg:
                await update.message.reply_text(msg)

    async def on_id(update: Any, context: Any) -> None:
        u = update.effective_user
        log.info("telegram id: %s %s (@%s)", u.id, u.full_name, u.username)
        await update.message.reply_text(f"{u.full_name}: your Telegram ID is {u.id}")

    app.add_handler(CommandHandler("id", on_id))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def _youtube_on_approve() -> bool:
    return bool(config.global_config().get("publish", {}).get("youtube_on_approve", True))


def _youtube_mode() -> str:
    return str(config.global_config().get("publish", {}).get("youtube_mode", "browser")).lower()


async def _publish(message: Any, db: ClipDB, clip_id: str) -> None:
    """Upload right after Approve and reply under the card with the link (or the error)."""
    import asyncio

    if _youtube_mode() == "api":
        from clipwatcher.publish import youtube as pub
    else:
        from clipwatcher.publish import youtube_browser as pub

    try:
        res = await asyncio.to_thread(pub.publish_clip, db, clip_id)
    except Exception as e:  # noqa: BLE001 - report every failure to the operators
        log.exception("youtube upload failed for %s", clip_id)
        await message.reply_text(f"❌ YouTube upload failed ({clip_id}): {e}")
        return
    if res.startswith("dryrun-"):
        why = (
            "DRY_RUN=1 without YT_BROWSER_FORCE=1 (set it to upload as private)"
            if _youtube_mode() != "api"
            else "DRY_RUN=1 or no YouTube API credentials"
        )
        await message.reply_text(f"🧪 Test mode: {clip_id} not uploaded ({why}).")
    else:
        what = "🔒 Uploaded as private (test mode)" if config.dry_run() else "📺 It's live on YouTube"
        await message.reply_text(f"{what}: {res}")


async def post_card(app: Any, db: ClipDB, clip_id: str, video: Path | None = None) -> None:
    chat_id = _chat_id()
    row = db.get(clip_id)
    if not row or not chat_id:
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=cb) for label, cb in r] for r in buttons_for(row)]
    )
    if video and Path(video).exists() and Path(video).stat().st_size < MAX_VIDEO_BYTES:
        attrs = video_attrs(Path(video))
        thumb = thumbnail(Path(video))
        with open(video, "rb") as f:
            # Without explicit width/height/duration Telegram's iOS player guesses the shape and stretches
            # the 9:16 video (the downloaded file is fine); the thumbnail keeps the preview vertical too.
            await app.bot.send_video(
                chat_id, f, caption=card_text(row), reply_markup=kb, supports_streaming=True,
                thumbnail=thumb.read_bytes() if thumb else None, **attrs,
                # the default few-second write timeout fires mid-upload of a 10-20 MB Short (18 Nov,
                # Windows server): the card arrived but the lane logged the clip as failed
                write_timeout=120, read_timeout=120, connect_timeout=30,
            )  # fmt: skip
        if thumb:
            thumb.unlink(missing_ok=True)
    else:
        await app.bot.send_message(chat_id, card_text(row), reply_markup=kb)


def video_attrs(video: Path) -> dict[str, int]:
    """width, height and duration (s) for send_video, from ffprobe; {} if probing fails."""
    import json
    import subprocess

    try:
        out = subprocess.run(
            [config.ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", str(video)],
            check=True, capture_output=True, text=True,
        ).stdout  # fmt: skip
        d = json.loads(out)
        st = d["streams"][0]
        return {"width": int(st["width"]), "height": int(st["height"]),
                "duration": round(float(d["format"]["duration"]))}  # fmt: skip
    except Exception:  # noqa: BLE001 - a card without size hints is still better than no card
        log.warning("ffprobe failed for %s", video)
        return {}


def thumbnail(video: Path) -> Path | None:
    """320 px tall JPEG from 1 s in (Telegram: <= 320 px, < 200 KB), or None."""
    import subprocess

    out = video.with_name(video.stem + "_tg_thumb.jpg")
    try:
        subprocess.run(
            [config.ffmpeg_bin(), "-y", "-v", "error", "-ss", "1", "-i", str(video), "-frames:v", "1",
             "-vf", "scale=-2:320", "-q:v", "5", str(out)],
            check=True, capture_output=True,
        )  # fmt: skip
        return out if out.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _chat_id() -> int | None:
    data = config._load("operators.yaml", "operators.example.yaml")
    return data.get("telegram_chat_id") or None


async def auto_publish(app: Any, db: ClipDB, clip_id: str) -> str | None:
    """publish.auto_approve (owner, 18 Nov): a clip scoring >= auto_approve_min is approved by the bot and
    uploaded at once; the link (or the error) goes to the operators' chat. None = not auto-approved."""
    import asyncio

    if not db.auto_approve_one(clip_id):
        return None
    staging.mark_approved(clip_id, "auto")
    if _youtube_mode() == "api":
        from clipwatcher.publish import youtube as pub
    else:
        from clipwatcher.publish import youtube_browser as pub
    chat_id = _chat_id()
    try:
        res = await asyncio.to_thread(pub.publish_clip, db, clip_id)
    except Exception as e:  # noqa: BLE001 - report every failure to the operators
        log.exception("auto upload failed for %s", clip_id)
        if chat_id and app is not None:
            await app.bot.send_message(chat_id, f"Auto-approved {clip_id}, YouTube upload failed: {e}")
        return None
    if chat_id and app is not None:
        privacy = "private (DRY_RUN=1)" if config.dry_run() else "public"
        what = f"not uploaded (dry run: {res})" if res.startswith("dryrun-") else f"YouTube {privacy}: {res}"
        await app.bot.send_message(chat_id, f"Auto-approved {clip_id}. {what}")
    return res


async def alert(app: Any, text: str) -> None:
    chat_id = _chat_id()
    if chat_id:
        await app.bot.send_message(chat_id, f"ALERT: {text}")


def main() -> None:
    """Run the approval bot (button presses, title edits). The live lane posts cards with its own client."""
    root = Path(os.environ.get("CLIPWATCHER_STAGING", config.ROOT / "staging"))
    root.mkdir(parents=True, exist_ok=True)
    build_application(ClipDB(root / "clips.db")).run_polling()


if __name__ == "__main__":
    main()
