"""Staging folders: staging/<clip_id>/, staging/approved/<clip_id>/, staging/published_clips.json."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .. import config
from .db import ClipDB

DEFAULT_ROOT = config.ROOT / "staging"


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
    root = Path(root) if root else DEFAULT_ROOT
    d = root / clip_id
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(short, d / "short_9x16.mp4")
    shutil.copy2(clean, d / "clean_16x9.mp4")
    meta = {**meta, "clip_id": clip_id, "state": "READY"}
    atomic_write_json(d / "meta.json", meta)
    if db is not None:
        db.add_clip(clip_id, str(meta.get("streamer", "")), meta, float(meta.get("score", 0) or 0))
    return d


def mark_approved(clip_id: str, approved_by: str, root: Path | None = None) -> Path:
    root = Path(root) if root else DEFAULT_ROOT
    src = root / clip_id
    dst = root / "approved" / clip_id
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file() and f.name != "meta.json":
            shutil.copy2(f, dst / f.name)
    meta = json.loads((src / "meta.json").read_text())
    meta.update(state="APPROVED", approved_by=approved_by)
    atomic_write_json(dst / "meta.json", meta)
    atomic_write_json(src / "meta.json", meta)
    return dst


def update_published(clip_id: str, platform: str, url: str, root: Path | None = None) -> dict[str, Any]:
    root = Path(root) if root else DEFAULT_ROOT
    p = root / "published_clips.json"
    data: dict[str, Any] = json.loads(p.read_text()) if p.exists() else {}
    data.setdefault(clip_id, {})[platform] = url
    atomic_write_json(p, data)
    return data
