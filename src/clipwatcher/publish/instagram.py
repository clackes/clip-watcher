"""Instagram Reels publisher (stub). Same APPROVED guard and DRY_RUN rules as YouTube."""

from __future__ import annotations

import os
import uuid
from typing import Any

from .. import config
from ..queue.db import APPROVED, ClipDB
from .youtube import NotApproved


def publish_reel(clip_id: str, meta: dict[str, Any], db: ClipDB, video_url: str) -> str:
    state = db.state(clip_id)
    if state != APPROVED:
        raise NotApproved(f"{clip_id} is {state}, not APPROVED")
    token = os.environ.get("INSTAGRAM_TOKEN")
    if config.dry_run() or not token:
        mid = f"dryrun-{uuid.uuid4().hex[:8]}"
        db.log("publisher", "instagram_dry_run", clip_id, mid)
        return mid
    # Outline of the live flow (Graph API):
    #   1. POST /{ig-user-id}/media  (media_type=REELS, video_url, caption) -> container id
    #   2. poll GET /{container-id}?fields=status_code until FINISHED
    #   3. POST /{ig-user-id}/media_publish (creation_id=container id) -> media id
    raise NotImplementedError("live Instagram publishing is not enabled yet")
