"""Cold open: replay the strongest reaction, zoomed in, before the clip starts (stops the scroll)."""

from __future__ import annotations

from clipwatcher import config

PRE_S = 0.6  # cold open starts this long before the reaction instant
MIN_INTO_CLIP_S = 3.0  # a reaction already in the first 3 s needs no cold open


def settings() -> tuple[float, float]:
    """(duration s, zoom factor) from render.cold_open_s / render.cold_open_zoom; duration 0 disables."""
    r = config.global_config().get("render", {})
    return float(r.get("cold_open_s", 2.0)), float(r.get("cold_open_zoom", 1.15))


def window(reaction_rel: float | None, trim_start: float, dur: float) -> tuple[float, float] | None:
    """(start, duration) of the cold open relative to the trimmed clip start, or None.

    reaction_rel is relative to the candidate window; trim_start/dur describe the trimmed clip.
    """
    co_s, _ = settings()
    if reaction_rel is None or co_s <= 0 or dur < co_s + MIN_INTO_CLIP_S:
        return None
    p = reaction_rel - trim_start
    if p < MIN_INTO_CLIP_S or p > dur:
        return None
    start = min(max(0.0, p - PRE_S), dur - co_s)
    return round(start, 2), co_s
