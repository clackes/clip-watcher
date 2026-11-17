"""YouTube publisher. Uploads only clips in state APPROVED; DRY_RUN never touches the network."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from .. import config
from ..queue.db import APPROVED, ClipDB

log = logging.getLogger(__name__)


class NotApproved(RuntimeError):
    pass


def _hashtags(meta: dict[str, Any]) -> list[str]:
    tags = ["#shorts"]
    if meta.get("game"):
        tags.append("#" + "".join(ch for ch in str(meta["game"]) if ch.isalnum()).lower())
    if meta.get("streamer"):
        tags.append("#" + str(meta["streamer"]).lower())
    return tags


def build_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    streamer = str(meta.get("streamer", ""))
    name = streamer.lower()
    from ..judge.compact import fallback_copy, is_bait

    seed = int(float(meta.get("start_s") or 0) * 1000)
    if not meta.get("description"):  # older packages: build the same English 4-line copy the judge writes
        plat = config.streamer(name).get("platform", "twitch")
        meta = {**meta, "description": fallback_copy(name, plat, meta.get("game"), seed)["ds"]}
    title = meta.get("title") or (meta.get("titles") or [None])[0]
    title = str(title or fallback_copy(name, "twitch", None, seed)["tt"]).strip()[:60].rstrip()
    campaign = meta.get("campaign_link")
    if meta.get("description"):  # judge copy: teaser + credit, question, reason to follow, hashtags
        lines = [ln for ln in str(meta["description"]).split("\n") if ln.strip() and not is_bait(ln)]
        if config.credit(name) not in meta["description"]:
            lines.insert(0, f"🎥 {streamer}: {config.credit(name)}")
        if campaign:
            lines.insert(len(lines) - 1, str(campaign))
        extra = [str(t) for t in meta.get("campaign_tags", []) if str(t) not in lines[-1].split()]
        if extra:  # paid-promotion tags (#ad) must be visible in the description, not only in tags
            lines[-1] = " ".join([lines[-1], *extra])
        tags = [t for t in lines[-1].split() if t.startswith("#")]
        return {
            "title": title,
            "description": "\n".join(lines),
            "tags": [t.lstrip("#") for t in tags],
            "paid_promotion": bool(campaign or meta.get("campaign_tags")),
        }
    lines = [f"🎥 {streamer}: {config.credit(name)}"]
    if meta.get("setup_line"):
        lines.append(str(meta["setup_line"]))
    if campaign:
        lines.append(str(campaign))
    tags = _hashtags(meta) + [str(t) for t in meta.get("campaign_tags", [])]
    lines.append(" ".join(tags))
    return {
        "title": title,
        "description": "\n".join(lines),
        "tags": [t.lstrip("#") for t in tags],
        "paid_promotion": bool(campaign or meta.get("campaign_tags")),
    }


def _service(creds_path: str) -> Any:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=Credentials.from_authorized_user_file(creds_path))


def upload(clip_id: str, meta: dict[str, Any], db: ClipDB, video: Path) -> str:
    """Upload the short for an APPROVED clip. Returns the YouTube video id."""
    state = db.state(clip_id)
    if state != APPROVED:
        raise NotApproved(f"{clip_id} is {state}, not APPROVED")
    md = build_metadata(meta)
    creds = os.environ.get("YOUTUBE_CREDENTIALS")
    if config.dry_run() and not creds:
        vid = f"dryrun-{uuid.uuid4().hex[:8]}"
        db.log("publisher", "youtube_dry_run", clip_id, f"private {vid}")
        return vid
    if not creds:
        raise RuntimeError("YOUTUBE_CREDENTIALS not set")
    from googleapiclient.http import MediaFileUpload

    privacy = "private" if config.dry_run() else "public"
    body = {
        "snippet": {
            "title": md["title"],
            "description": md["description"],
            "tags": md["tags"],
            "categoryId": "20",
            # English-only channel: tells YouTube which audience to test the Short with
            "defaultLanguage": config.channel_language(),
            "defaultAudioLanguage": config.channel_language(),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": False,
            "paidProductPlacement": md["paid_promotion"],
        },
    }
    yt = _service(creds)
    req = yt.videos().insert(
        part="snippet,status", body=body, media_body=MediaFileUpload(str(video), resumable=True)
    )
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    got = yt.videos().list(part="status", id=vid).execute()
    actual = got["items"][0]["status"]["privacyStatus"] if got.get("items") else None
    if actual != privacy:
        db.log("publisher", "youtube_privacy_mismatch", clip_id, f"want={privacy} got={actual}")
        log.warning("privacy mismatch for %s: want %s got %s", vid, privacy, actual)
    db.set_state(clip_id, "PUBLISHED", actor="publisher", youtube_id=vid)
    return vid


def publish_clip(db: ClipDB, clip_id: str, root: Path | None = None) -> str:
    """Upload an APPROVED clip's short from staging/approved. Returns a watch URL (or 'dryrun-…' id).

    Title edits made in Telegram live in the DB row's meta and win over the staged meta.json.
    """
    import json

    from ..queue import staging

    root = Path(root) if root else staging.DEFAULT_ROOT
    folder = root / "approved" / clip_id
    meta = json.loads((folder / "meta.json").read_text())
    row = db.get(clip_id) or {}
    meta.update(json.loads(row.get("meta_json") or "{}"))
    vid = upload(clip_id, meta, db, folder / "short_9x16.mp4")
    if vid.startswith("dryrun-"):
        return vid
    url = f"https://youtube.com/shorts/{vid}"
    staging.update_published(clip_id, "youtube", url, root)
    return url
