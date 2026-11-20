"""The Library place of the companion app: the requests of /api/library/... (see server.PLACES)."""

from __future__ import annotations

from typing import Any

from clipwatcher.ui import NotFound


def get(app: Any, parts: list[str], query: dict[str, list[str]]) -> Any:
    raise NotFound("Not found.")


def post(app: Any, parts: list[str], body: dict[str, Any]) -> Any:
    raise NotFound("Not found.")
