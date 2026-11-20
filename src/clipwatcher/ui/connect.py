"""The Connect place of the companion app: the requests of /api/connect/... (see server.PLACES)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clipwatcher.ui import NotFound


def get(app: Any, parts: list[str], query: dict[str, list[str]]) -> Any:
    raise NotFound("Not found.")


def post(app: Any, parts: list[str], body: dict[str, Any]) -> Any:
    raise NotFound("Not found.")


def image(image_id: str) -> tuple[Path, str] | None:
    """The picture of a connected account by its id: the file and its content type, or None."""
    return None
