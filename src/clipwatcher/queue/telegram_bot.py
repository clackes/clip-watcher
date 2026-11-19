"""Telegram approval queue. Button logic is pure; python-telegram-bot is imported lazily."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from .. import config
from . import staging
from .db import CARD_MAX_ATTEMPTS, ClipDB

log = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 50 * 1024 * 1024
CAPTION_MAX, TEXT_MAX = 1024, 4096  # Telegram limits for a video caption and a text message
CARD_LEASE_S = 300  # how long one attempt to post a card keeps every other poster away
CARD_GRACE_S = 120  # the lane that staged a clip posts its card itself; the bot only steps in after this
UPLOADING = "Uploading to YouTube."


def operator_for(telegram_id: int, operators: dict[str, Any] | None = None) -> str | None:
    """Operator name for a Telegram user id, or None (ignored)."""
    ops = config.operators() if operators is None else operators
    for name, o in ops.items():
        tid = o.get("telegram_id")
        if tid and int(tid) == int(telegram_id):
            return name
    return None


def claim_expired(row: dict[str, Any] | None, now: float | None = None) -> bool:
    """True for a CLAIMED row whose TTL ran out: the holder lost the lock and anyone may claim the clip."""
    if not row or row.get("state") != "CLAIMED":
        return False
    return float(row.get("claim_until") or 0) < (time.time() if now is None else now)


def card_text(row: dict[str, Any]) -> str:
    import json

    meta = json.loads(row.get("meta_json") or "{}")
    lines = [f"Clip {row['id']} - {row['streamer']}", f"State: {row['state']}"]
    if claim_expired(row):
        lines.append(f"Claim by {row['claimed_by']} expired: free to claim")
    elif row.get("claimed_by"):
        lines.append(f"Held by: {row['claimed_by']}")
    lines += _youtube_lines(row)
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


def _youtube_lines(row: dict[str, Any]) -> list[str]:
    """Where the clip is on YouTube, or why an APPROVED clip is not there yet."""
    url = f"https://youtube.com/shorts/{row['youtube_id']}" if row.get("youtube_id") else None
    if row["state"] == "PUBLISHED":
        return [f"YouTube: {url}"] if url else []
    if row["state"] != "APPROVED":
        return []
    lines = []
    if row.get("publish_error"):
        lines.append(f"⚠️ YouTube upload failed: {row['publish_error']}")
    if url:  # the upload got a video id and then did not finish: it may be there as a draft, or even live
        lines.append(f"⚠️ An earlier upload reached YouTube as {url}: check YouTube Studio before retrying")
    return lines


def upload_unfinished(row: dict[str, Any]) -> bool:
    """An APPROVED clip whose upload failed or stopped half way: an operator decides about the retry."""
    return row["state"] == "APPROVED" and bool(row.get("publish_error") or row.get("youtube_id"))


def buttons_for(row: dict[str, Any], operators: dict[str, Any] | None = None) -> list[list[tuple[str, str]]]:
    """Rows of (label, callback_data). READY or an expired claim -> one Claim button per operator;
    CLAIMED -> holder actions; APPROVED with an upload that did not go through -> Retry."""
    cid = row["id"]
    if upload_unfinished(row):
        return [[("Retry YouTube upload", f"publish:{cid}")]]
    if row["state"] == "READY" or claim_expired(row):
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
        row = db.get(clip_id)
        if ok:
            return "Claimed.", row
        return (f"Already held by {holder}." if holder else _refusal(row, "claim")), row
    if action == "approve":
        ok = db.approve(clip_id, op)
        if ok:
            try:
                staging.mark_approved(clip_id, op, root, db=db)
            except OSError as e:  # the approval stands; the upload reports the missing package on the card
                log.warning("approved package of %s not built: %s", clip_id, e)
        row = db.get(clip_id)
        return ("Approved." if ok else _refusal(row, "approve")), row
    if action == "publish":  # Retry on the card of an APPROVED clip; any operator may press it
        row = db.get(clip_id)
        if row is None or row["state"] != "APPROVED":
            state = row["state"].lower() if row else ""
            return (f"Nothing to upload: the clip is {state}." if row else _refusal(row, "upload")), row
        db.log(op, "publish_retry", clip_id)
        return UPLOADING, row
    if action == "reject":
        ok = db.reject(clip_id, op)
        row = db.get(clip_id)
        return ("Rejected." if ok else _refusal(row, "reject")), row
    if action == "release":
        ok = db.release(clip_id, op)
        row = db.get(clip_id)
        return ("Released." if ok else _refusal(row, "release")), row
    if action == "edit":
        row = db.get(clip_id)
        if not row or row["state"] != "CLAIMED" or row["claimed_by"] != op or claim_expired(row):
            return _refusal(row, "edit"), row
        return "Reply with the new title.", row
    return "Unknown action.", db.get(clip_id)


def _refusal(row: dict[str, Any] | None, verb: str) -> str:
    """Why a holder action was refused. The card is redrawn from the same row, so after an expired claim it
    shows the Claim buttons again."""
    if row is None:
        return "This clip is no longer in the queue."
    if claim_expired(row):
        return f"The claim expired. Claim the clip again to {verb} it."
    if row["state"] == "READY":
        return f"Claim the clip first to {verb} it."
    if row["state"] != "CLAIMED":
        return f"Already {row['state'].lower()}."
    return f"Only the holder can {verb}."


def handle_edit_reply(
    db: ClipDB, telegram_id: int, clip_id: str, text: str, operators: dict[str, Any] | None = None
) -> bool:
    op = operator_for(telegram_id, operators)
    if op is None:
        return False
    return db.edit(clip_id, op, {"title": text.strip()[:60]})


def build_application(db: ClipDB, token: str | None = None, root: Path | None = None) -> Any:
    """Create the python-telegram-bot Application (lazy import). root: staging folder (staging_root()).

    run_polling() (the approval bot) also starts housekeeping() and resume_publishing(); a caller that only
    initialize()s the application to post cards (the live lane) does not.
    """
    import asyncio

    from telegram import ForceReply
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

    async def resume(application: Any) -> None:
        try:
            await resume_publishing(application, db, root)
        except Exception:  # noqa: BLE001 - the bot still has to come up; Retry buttons remain
            log.exception("resuming uploads after the restart failed")

    async def started(application: Any) -> None:
        application.bot_data["tasks"] = [
            asyncio.create_task(housekeeping(application, db, root)),
            asyncio.create_task(resume(application)),
        ]

    async def stopping(application: Any) -> None:
        tasks = application.bot_data.pop("tasks", [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    token = token or os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).post_init(started).post_stop(stopping).build()

    async def on_button(update: Any, context: Any) -> None:
        q = update.callback_query
        text, row = await press(context.application, db, q, root=root)
        if row is not None and q.data.startswith("edit:") and text.startswith("Reply"):
            await q.message.reply_text(f"Edit {row['id']}: send the new title.", reply_markup=ForceReply())
            context.user_data["editing"] = row["id"]

    async def on_text(update: Any, context: Any) -> None:
        cid = context.user_data.pop("editing", None)
        if not cid:
            return
        if handle_edit_reply(db, update.effective_user.id, cid, update.message.text):
            await update.message.reply_text("Title updated.")
            await refresh_card(context.application, db, cid)
        else:  # the claim ran out or the clip moved on while the title was being typed
            await update.message.reply_text(f"Title not saved. {_refusal(db.get(cid), 'edit')}")

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


def _publisher() -> Any:
    if _youtube_mode() == "api":
        from ..publish import youtube as pub
    else:
        from ..publish import youtube_browser as pub
    return pub


def _starts_upload(data: str, text: str) -> bool:
    action = data.split(":")[0]
    return (action == "approve" and text == "Approved." and _youtube_on_approve()) or (
        action == "publish" and text == UPLOADING
    )


async def press(
    app: Any, db: ClipDB, q: Any, *, root: Path | None = None
) -> tuple[str, dict[str, Any] | None]:
    """One button press: the queue action, the answer, the redrawn card, then the upload if one is due.

    Telegram failing on the answer or on the card must not cost an approved clip its upload, so neither can
    raise here and the upload is started last, whatever happened before. (Last and not first: an upload that
    fails at once must redraw the card after this press has, or its Retry button would be painted over.)
    """
    text, row = handle_action(db, q.from_user.id, q.data, root=root)
    if not text:
        return text, row
    try:
        try:
            await q.answer(text)
        except Exception as e:  # noqa: BLE001
            log.warning("answer to %s not delivered: %s", q.data, e)
        if row is not None:
            _remember_card(db, row, q.message)
            await refresh_card(app, db, row["id"], query=q)
    finally:
        if row is not None and _starts_upload(q.data, text):
            app.create_task(publish(app, db, row["id"], root=root, query=q))
    return text, row


def _upload(db: ClipDB, clip_id: str, root: Path | None) -> str:
    """The upload itself, in a worker thread. Builds the approved package first when the approval could
    not (its failure is recorded like an upload failure), so a Retry can succeed."""
    from ..publish.youtube import failure_recorded

    row = db.get(clip_id) or {}
    base = Path(root) if root else staging.staging_root()
    if row.get("state") == "APPROVED" and not (base / "approved" / clip_id / "meta.json").exists():
        with failure_recorded(db, clip_id):
            staging.mark_approved(clip_id, row.get("approved_by") or "", base, db=db)
    return _publisher().publish_clip(db, clip_id, root)


async def publish(app: Any, db: ClipDB, clip_id: str, *, root: Path | None = None, query: Any = None) -> None:
    """Upload an APPROVED clip to YouTube and tell the operators how it went: the card is redrawn (PUBLISHED
    with the link, or the reason and a Retry button) and a line goes to the chat. Never raises.

    One upload at a time (the browser publisher drives one Chrome profile), and never two of the same clip.
    """
    import asyncio

    from ..publish.youtube import NotApproved

    running = app.bot_data.setdefault("uploading", set())
    if clip_id in running:
        return
    running.add(clip_id)
    try:
        async with app.bot_data.setdefault("upload_lock", asyncio.Lock()):
            res = await asyncio.to_thread(_upload, db, clip_id, root)
    except NotApproved as e:  # published or rejected in the meantime: nothing was uploaded
        note = f"{clip_id} not uploaded: {e}"
    except Exception as e:  # noqa: BLE001 - every failure goes to the operators (the publisher audited it)
        log.exception("youtube upload failed for %s", clip_id)
        note = f"YouTube upload failed for {clip_id}: {e}\nRetry with the button on its card."
    else:
        if res.startswith("dryrun-"):
            why = "no YouTube credentials set" if _youtube_mode() == "api" else "DRY_RUN=1"
            note = f"DRY RUN: {clip_id} not uploaded ({why})."
        else:
            privacy = "private (DRY_RUN=1)" if config.dry_run() else "public"
            note = f"YouTube {privacy}: {res}"
    finally:
        running.discard(clip_id)
    await refresh_card(app, db, clip_id, query=query)
    await _tell(app, db, clip_id, note, query)


async def _tell(app: Any, db: ClipDB, clip_id: str, text: str, query: Any = None) -> None:
    """A line for the operators: under the pressed card, else in the chat the clip's card is in."""
    try:
        if query is not None and query.message is not None:
            await query.message.reply_text(text)
            return
        chat_id = (db.get(clip_id) or {}).get("card_chat_id") or _chat_id()
        if chat_id:
            await app.bot.send_message(chat_id, text)
    except Exception as e:  # noqa: BLE001 - the card and the audit log carry the same news
        log.warning("not sent to the chat (%s): %s", e, text)


async def resume_publishing(app: Any, db: ClipDB, root: Path | None = None) -> list[str]:
    """After a restart: upload the clips that were approved but never got to YouTube because the bot stopped
    before or during the upload, or the upload failed. Only with publish.youtube_on_approve on.

    Not uploaded here: a clip whose earlier attempt already got a video id (it may be on YouTube; its card
    says so and offers Retry, an operator decides), a clip whose dry run went through, and approvals older
    than publish.resume_max_h (24).
    """
    if not _youtube_on_approve():
        return []
    max_h = float(config.global_config().get("publish", {}).get("resume_max_h", 24))
    resumed = []
    for row in db.list_clips("APPROVED"):
        clip_id = row["id"]
        if float(row.get("updated_at") or 0) < time.time() - max_h * 3600:
            continue
        if row.get("youtube_id"):
            await refresh_card(app, db, clip_id)
            continue
        last = db.last_audit(clip_id, "youtube%")
        if last is not None and last["action"].endswith("dry_run"):
            continue
        log.info("%s was approved but not uploaded: uploading now", clip_id)
        await publish(app, db, clip_id, root=root)
        resumed.append(clip_id)
    return resumed


def _markup(rows: list[list[tuple[str, str]]]) -> Any:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=cb) for label, cb in r] for r in rows]
    )


def _fit(text: str, limit: int) -> str:
    """Telegram refuses a caption over 1024 or a message over 4096 characters: cut it, keep the card."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def post_card(app: Any, db: ClipDB, clip_id: str, video: Path | None = None) -> bool:
    """Post the approval card of a clip and remember where it is. True when a card was posted.

    Safe to call again for the same clip: the queue hands out the card once (atomic reservation), so a second
    call while the clip has a card, or while the live lane or the approval bot is sending it, posts nothing.
    A send error is raised; the reservation then runs out after CARD_LEASE_S and post_missing_cards() tries
    again (CARD_MAX_ATTEMPTS in all, the last one without the video).
    """
    chat_id = _chat_id()
    row = db.get(clip_id)
    if not row or not chat_id:
        return False
    attempt = db.reserve_card(clip_id, CARD_LEASE_S)
    if not attempt:
        return False
    kb = _markup(buttons_for(row))
    has_video = bool(video) and Path(video).exists() and Path(video).stat().st_size < MAX_VIDEO_BYTES
    if has_video and attempt < CARD_MAX_ATTEMPTS:
        attrs = video_attrs(Path(video))
        thumb = thumbnail(Path(video))
        try:
            with open(video, "rb") as f:
                # Without explicit width/height/duration Telegram's iOS player guesses the shape and stretches
                # the 9:16 video (the downloaded file is fine); the thumbnail keeps the preview vertical too.
                # Long timeouts: the answer to a 30 MB upload can take a while, and a card that timed out here
                # but did arrive would be posted a second time by the retry.
                msg = await app.bot.send_video(
                    chat_id, f, caption=_fit(card_text(row), CAPTION_MAX), reply_markup=kb,
                    supports_streaming=True, thumbnail=thumb.read_bytes() if thumb else None,
                    read_timeout=60, write_timeout=120, **attrs,
                )  # fmt: skip
        finally:
            if thumb:
                thumb.unlink(missing_ok=True)
    else:
        has_video = False
        msg = await app.bot.send_message(chat_id, _fit(card_text(row), TEXT_MAX), reply_markup=kb)
    db.set_card(clip_id, chat_id, msg.message_id, has_video)
    return True


async def post_missing_cards(app: Any, db: ClipDB, root: Path | None = None) -> list[str]:
    """Post a card for every READY clip that has none: the lane's own post_card failed, the lane was
    restarted between staging and posting, or the clip came from a lane without a bot (VOD, worker).
    Clips queued more than publish.card_max_age_h ago (24; 0 turns this off) are left alone."""
    max_age_h = float(config.global_config().get("publish", {}).get("card_max_age_h", 24))
    if max_age_h <= 0:
        return []
    root = Path(root) if root else staging.staging_root()
    posted = []
    for row in db.cardless(CARD_GRACE_S, max_age_h * 3600):
        try:
            if await post_card(app, db, row["id"], root / row["id"] / "short_9x16.mp4"):
                posted.append(row["id"])
        except Exception:  # noqa: BLE001 - one card that cannot be sent must not hold up the others
            log.exception("card for %s not posted", row["id"])
    return posted


async def refresh_card(app: Any, db: ClipDB, clip_id: str, *, query: Any = None) -> bool:
    """Redraw a clip's card (text and buttons) from its queue row.

    query: the button press being answered, whose message is edited; without it the card recorded by
    post_card. Never raises: a card that cannot be edited must not stop a claim, an approval or an upload.
    """
    row = db.get(clip_id)
    if not row:
        return False
    text, kb = card_text(row), _markup(buttons_for(row))
    try:
        if query is not None:
            # Video cards carry the text as a caption; text-only cards as message text.
            if query.message is not None and query.message.text is None:
                await query.edit_message_caption(_fit(text, CAPTION_MAX), reply_markup=kb)
            else:
                await query.edit_message_text(_fit(text, TEXT_MAX), reply_markup=kb)
        elif row.get("card_message_id"):  # None: no card yet; LEGACY_CARD (0): posted before ids were kept
            at = {"chat_id": row["card_chat_id"], "message_id": row["card_message_id"]}
            if row.get("card_video"):
                await app.bot.edit_message_caption(caption=_fit(text, CAPTION_MAX), reply_markup=kb, **at)
            else:
                await app.bot.edit_message_text(_fit(text, TEXT_MAX), reply_markup=kb, **at)
        else:
            return False
    except Exception as e:  # noqa: BLE001
        if "not modified" in str(e).lower():  # the card already shows this
            return True
        log.warning("card of %s not updated: %s", clip_id, e)
        return False
    return True


def _remember_card(db: ClipDB, row: dict[str, Any], message: Any) -> None:
    """A card posted before the queue kept card ids tells us where it is when a button on it is pressed."""
    if row.get("card_message_id") or message is None:
        return
    try:
        video = getattr(message, "text", None) is None
        db.set_card(row["id"], message.chat.id, message.message_id, video)
    except Exception:  # noqa: BLE001 - only a convenience for later redraws
        log.warning("card of %s not recorded", row["id"])


async def sweep(app: Any, db: ClipDB, root: Path | None = None) -> None:
    """One housekeeping pass of the approval bot: cards of claims that ran out go back to the Claim buttons,
    READY clips without a card get one."""
    for clip_id in db.release_expired():
        await refresh_card(app, db, clip_id)
    await post_missing_cards(app, db, root)


async def housekeeping(app: Any, db: ClipDB, root: Path | None = None, every_s: float = 60.0) -> None:
    """Run sweep() for as long as the approval bot runs (started by build_application's post_init)."""
    import asyncio

    while True:
        try:
            await sweep(app, db, root)
        except Exception:  # noqa: BLE001 - the next pass tries again
            log.exception("housekeeping pass failed")
        await asyncio.sleep(every_s)


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
    """Run the approval bot: button presses, title edits and housekeeping(). The live lane posts its cards
    with its own client; the bot posts the ones that went missing."""
    root = staging.staging_root()
    root.mkdir(parents=True, exist_ok=True)
    build_application(ClipDB(root / "clips.db"), root=root).run_polling()


if __name__ == "__main__":
    main()
