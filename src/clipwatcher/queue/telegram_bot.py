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
    for k in ("title", "hook", "setup_line"):
        if meta.get(k):
            lines.append(f"{k}: {meta[k]}")
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
    from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

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
        if q.data.startswith("edit:") and text.startswith("Reply"):
            await q.message.reply_text(f"Edit {row['id']}: send the new title.", reply_markup=ForceReply())
            context.user_data["editing"] = row["id"]

    async def on_text(update: Any, context: Any) -> None:
        cid = context.user_data.pop("editing", None)
        if cid and handle_edit_reply(db, update.effective_user.id, cid, update.message.text):
            await update.message.reply_text("Title updated.")

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


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
        with open(video, "rb") as f:
            await app.bot.send_video(chat_id, f, caption=card_text(row), reply_markup=kb)
    else:
        await app.bot.send_message(chat_id, card_text(row), reply_markup=kb)


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
