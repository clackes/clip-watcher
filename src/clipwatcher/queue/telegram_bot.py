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


def card_text(row: dict[str, Any]) -> str:
    import json

    meta = json.loads(row.get("meta_json") or "{}")
    lines = [f"Clip {row['id']} - {row['streamer']}", f"State: {row['state']}"]
    if row.get("claimed_by"):
        lines.append(f"Held by: {row['claimed_by']}")
    for k in ("title", "hook", "why"):
        if meta.get(k):
            lines.append(f"{k}: {meta[k]}")
    if meta.get("judge_score"):
        lines.append(f"score: {meta['judge_score']}/10")
    flagged = [k for k, v in (meta.get("flags") or {}).items() if v]
    if flagged:
        lines.append("⚠️ flags: " + ", ".join(flagged))
    if meta.get("description"):
        lines.append("\n" + str(meta["description"]))
    return "\n".join(lines)


def buttons_for(row: dict[str, Any], operators: dict[str, Any] | None = None) -> list[list[tuple[str, str]]]:
    """Rows of (label, callback_data). READY -> one Claim button per operator; CLAIMED -> holder actions."""
    cid = row["id"]
    if row["state"] == "READY":
        ops = config.operators() if operators is None else operators
        return [[(f"Claim: {o.get('label', n)}", f"claim:{cid}:{n}") for n, o in ops.items()]]
    if row["state"] == "CLAIMED":
        return [
            [("Approve", f"approve:{cid}"), ("Edit", f"edit:{cid}")],
            [("Reject", f"reject:{cid}"), ("Release", f"release:{cid}")],
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
            return "That claim button is not yours.", db.get(clip_id)
        ok, holder = db.claim(clip_id, op)
        return ("Claimed." if ok else f"Already held by {holder}."), db.get(clip_id)
    if action == "approve":
        ok = db.approve(clip_id, op)
        if ok:
            try:
                staging.mark_approved(clip_id, op, root)
            except FileNotFoundError:
                log.warning("staging folder missing for %s", clip_id)
        return ("Approved." if ok else "Only the holder can approve."), db.get(clip_id)
    if action == "reject":
        ok = db.reject(clip_id, op)
        return ("Rejected." if ok else "Only the holder can reject."), db.get(clip_id)
    if action == "release":
        ok = db.release(clip_id, op)
        return ("Released." if ok else "Only the holder can release."), db.get(clip_id)
    if action == "edit":
        row = db.get(clip_id)
        if not row or row["claimed_by"] != op:
            return "Only the holder can edit.", row
        return "Reply with the new title.", row
    return "Unknown action.", db.get(clip_id)


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
        if q.data.startswith("approve:") and text == "Approved." and _youtube_on_approve():
            context.application.create_task(_publish(q.message, db, row["id"]))
        if q.data.startswith("edit:") and text.startswith("Reply"):
            await q.message.reply_text(f"Edit {row['id']}: send the new title.", reply_markup=ForceReply())
            context.user_data["editing"] = row["id"]

    async def on_text(update: Any, context: Any) -> None:
        cid = context.user_data.pop("editing", None)
        if cid and handle_edit_reply(db, update.effective_user.id, cid, update.message.text):
            await update.message.reply_text("Title updated.")

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


async def _publish(message: Any, db: ClipDB, clip_id: str) -> None:
    """Upload right after Approve and reply under the card with the link (or the error)."""
    import asyncio

    from clipwatcher.publish import youtube

    try:
        res = await asyncio.to_thread(youtube.publish_clip, db, clip_id)
    except Exception as e:  # noqa: BLE001 - report every failure to the operators
        log.exception("youtube upload failed for %s", clip_id)
        await message.reply_text(f"YouTube upload failed for {clip_id}: {e}")
        return
    if res.startswith("dryrun-"):
        await message.reply_text(f"DRY RUN: {clip_id} not uploaded (no YouTube credentials set).")
    else:
        privacy = "private (DRY_RUN=1)" if config.dry_run() else "public"
        await message.reply_text(f"YouTube {privacy}: {res}")


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
