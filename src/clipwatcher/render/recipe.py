"""What a Short was rendered with, kept beside it in its staging folder (recipe.json): the words, the webcam
and gameplay rectangles, the judge's beats and labels. With it the same clip can be rendered again, with
another caption style or layout, from its clean cut, without transcribing or looking for the webcam again.

Times are relative to the start of the clip, as `render_short` takes them when the clean cut is its source.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from clipwatcher.models import Word

log = logging.getLogger(__name__)
NAME = "recipe.json"
_RECTS = ("cam", "game", "face")


def keep(
    folder: Path,
    *,
    words: list[Word],
    hook: str | None,
    layout: dict[str, Any],
    beats: list[tuple] | None = None,
    actions: list[tuple] | None = None,
    cold_open: tuple[float, float] | None = None,
) -> None:
    """Write the recipe into a staged clip's folder. Never fails a clip: a recipe that cannot be written is
    logged, and the clip is then rendered again from what can be worked out of its video."""
    try:
        recipe = {
            "words": [[round(w.start, 3), round(w.end, 3), w.text] for w in words],
            "hook": hook,
            "layout": layout,
            "beats": [list(b) for b in beats or []],
            "actions": [list(a) for a in actions or []],
            "cold_open": list(cold_open) if cold_open else None,
        }
        tmp = Path(folder) / (NAME + ".tmp")
        tmp.write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
        tmp.replace(Path(folder) / NAME)
    except (OSError, TypeError, ValueError, AttributeError) as e:
        log.warning("recipe of %s not kept: %s", folder, e)


def read(folder: Path) -> dict[str, Any] | None:
    """The keyword arguments `render_short` was given for this clip (words, hook, has_webcam, cam, game,
    face, reframe, beats, actions, cold_open), or None when the folder holds no recipe."""
    try:
        r = json.loads((Path(folder) / NAME).read_text(encoding="utf-8"))
        layout = dict(r.get("layout") or {})
        for k in _RECTS:  # a rectangle is a tuple (x, y, w, h) or an ffmpeg crop text; JSON made it a list
            if isinstance(layout.get(k), list):
                layout[k] = tuple(layout[k])
        co = r.get("cold_open")
        return {
            "words": [Word(float(s), float(e), str(t)) for s, e, t in r.get("words") or []],
            "hook": r.get("hook"),
            **layout,
            "beats": [tuple(b) for b in r.get("beats") or []],
            "actions": [tuple(a) for a in r.get("actions") or []],
            "cold_open": (float(co[0]), float(co[1])) if co else None,
        }
    except (OSError, ValueError, TypeError, KeyError):
        return None
