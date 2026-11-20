"""Staging folders: staging/<clip_id>/, staging/approved/<clip_id>/, staging/published_clips.json."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .. import config
from .db import ClipDB

log = logging.getLogger(__name__)
DEFAULT_ROOT = config.ROOT / "staging"


def staging_root() -> Path:
    """The one staging folder the lanes, the approval bot and the publishers share:
    CLIPWATCHER_STAGING when set (Docker, VPS), else <repo>/staging."""
    env = os.environ.get("CLIPWATCHER_STAGING")
    return Path(env).expanduser() if env else DEFAULT_ROOT


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def stage_package(
    clip_id: str,
    short: Path,
    clean: Path,
    meta: dict[str, Any],
    root: Path | None = None,
    db: ClipDB | None = None,
) -> Path:
    """Copy a clip into staging/<clip_id>/ and queue it as READY.

    Clip ids are deterministic, so a VOD re-run stages the same ids again: a clip that is still READY gets the
    new files and meta; one an operator already claimed, approved or rejected, or that was published, is left
    exactly as it is (its folder is returned).
    """
    root = Path(root) if root else staging_root()
    d = root / clip_id
    decided = _decided_state(clip_id, root, db)
    if decided:
        log.warning("%s is already %s: staged package left as it is", clip_id, decided)
        if db is not None:
            db.log("system", "restage_refused", clip_id, f"state={decided}")
        return d
    d.mkdir(parents=True, exist_ok=True)
    _copy_atomic(short, d / "short_9x16.mp4")
    _copy_atomic(clean, d / "clean_16x9.mp4")
    meta = {**meta, "clip_id": clip_id, "state": "READY"}
    atomic_write_json(d / "meta.json", meta)
    if db is not None:
        db.add_clip(clip_id, str(meta.get("streamer", "")), meta, float(meta.get("score", 0) or 0))
    return d


def _decided_state(clip_id: str, root: Path, db: ClipDB | None) -> str | None:
    """State of a clip that is past READY (must not be re-staged), else None. The queue row decides; without
    one the folders do (approved/<clip_id>/, the state in the staged meta.json)."""
    state = db.state(clip_id) if db is not None else None
    if state is None:
        if (root / "approved" / clip_id).exists():
            return "APPROVED"
        try:
            state = json.loads((root / clip_id / "meta.json").read_text()).get("state")
        except (OSError, ValueError):
            return None
    return state if state and state != "READY" else None


def _copy_atomic(src: Path, dst: Path) -> None:
    """Copy to a temp name, then rename: a replaced file gets a new inode (a hard link in approved/ keeps the
    approved video) and a crash never leaves half a video in the package."""
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hard link (no second copy of every approved video on disk); copy across filesystems."""
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def mark_approved(clip_id: str, approved_by: str, root: Path | None = None, db: ClipDB | None = None) -> Path:
    """Build staging/approved/<clip_id>/ (what the publisher uploads and people post by hand from).
    db: the queue; what an operator edited in Telegram (the title) lives in its row and goes into meta.json.
    """
    root = Path(root) if root else staging_root()
    src = root / clip_id
    dst = root / "approved" / clip_id
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file() and f.name != "meta.json" and f.suffix != ".tmp":
            _link_or_copy(f, dst / f.name)
    meta = json.loads((src / "meta.json").read_text())
    if db is not None:
        meta.update(json.loads((db.get(clip_id) or {}).get("meta_json") or "{}"))
    meta.update(state="APPROVED", approved_by=approved_by)
    atomic_write_json(dst / "meta.json", meta)
    atomic_write_json(src / "meta.json", meta)
    return dst


def update_published(clip_id: str, platform: str, url: str, root: Path | None = None) -> dict[str, Any]:
    root = Path(root) if root else staging_root()
    p = root / "published_clips.json"
    data: dict[str, Any] = json.loads(p.read_text()) if p.exists() else {}
    data.setdefault(clip_id, {})[platform] = url
    atomic_write_json(p, data)
    return data
