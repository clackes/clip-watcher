"""Disk helpers."""

from __future__ import annotations

import shutil
from pathlib import Path


def free_pct(path: str | Path) -> float:
    """Percent of the filesystem holding `path` that is free (0-100)."""
    u = shutil.disk_usage(path)
    return 100.0 * u.free / u.total if u.total else 0.0
