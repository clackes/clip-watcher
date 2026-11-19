"""Snap judge trim points so clips never start or end mid-word, and end on a finished sentence.

Words come from Whisper with punctuation attached, times relative to the candidate window.
No extra audio pass: the live lane cannot afford one, and word gaps already mark the pauses.
"""

from __future__ import annotations

from clipwatcher import config
from clipwatcher.models import Word

LEAD_S = 0.15  # silence kept before the first word
TAIL_S = 0.35  # silence kept after the last word
END_FWD_S = 3.0  # extend the end to finish the sentence at most this far
_STOPS = (".", "?", "!", "…")


def _ends_sentence(w: Word) -> bool:
    return w.text.strip().rstrip("\"')]").endswith(_STOPS)


def snap_trim(t0: float, t1: float, words: list[Word], dur: float) -> tuple[float, float]:
    """Return (t0, t1) moved to clean cut points inside [0, dur]. Unchanged when there are no words."""
    ws = sorted((w for w in words if w.text.strip() and w.end > w.start), key=lambda w: w.start)
    if not ws or t1 <= t0:
        return t0, t1
    s, e = t0, t1

    # Start: never inside a word, but never earlier than needed either. Viewers swipe in the first second,
    # so the start is not pulled back to a sentence start (the judge already starts close to the action).
    i = next((k for k, w in enumerate(ws) if w.end > s), None)
    if i is not None:
        first = ws[i]
        if first.start < s or first.start - s < LEAD_S:
            prev_end = ws[i - 1].end if i > 0 else 0.0
            s = max(prev_end, first.start - LEAD_S, 0.0)

    # End: last word that started before t1, extended to the end of its sentence if that is close.
    j = max((k for k, w in enumerate(ws) if w.start < e), default=None)
    if j is not None:
        if not _ends_sentence(ws[j]):
            for k in range(j + 1, len(ws)):
                if ws[k].end - e > END_FWD_S:
                    break
                if _ends_sentence(ws[k]):
                    j = k
                    break
        last = ws[j]
        if last.end > e or e - last.end < TAIL_S:
            nxt = ws[j + 1].start if j + 1 < len(ws) else dur
            e = min(max(e, last.end + TAIL_S), max(nxt, last.end), dur)

    s, e = max(0.0, s), min(float(dur), e)
    if e - s < 1.0:
        return t0, t1
    return round(s, 2), round(e, 2)


HEURISTIC_LEAD_S = 10.0  # fallback start: this far before the reaction
CHAT_LAG_S = 3.0  # chat spikes this long after what viewers saw


def reaction_s(judge_peak: float | None, chat_peak_rel: float) -> float:
    """Strongest reaction relative to the candidate window: the judge's pk, else the chat peak minus lag."""
    return float(judge_peak) if judge_peak is not None else max(0.0, chat_peak_rel - CHAT_LAG_S)


def clip_bounds() -> tuple[float, float]:
    """(shortest, longest) clip in seconds: render.clip_min_s / clip_max_s (owner, 19 Nov: 30-59 s)."""
    r = config.global_config().get("render", {})
    return float(r.get("clip_min_s", 12.0)), float(r.get("clip_max_s", 59.0))


def fit_length(t0: float, t1: float, dur: float) -> tuple[float, float]:
    """Bring a trim inside the allowed clip length without leaving [0, dur].

    Too short: more setup first (start earlier), then more aftermath. Too long: the oldest part goes, so the
    clip still ends on the punchline.
    """
    lo, hi = clip_bounds()
    if t1 - t0 > hi:
        t0 = t1 - hi
    if t1 - t0 < lo:
        t0 = max(0.0, t1 - lo)
        t1 = min(float(dur), t0 + lo)
    return round(t0, 2), round(t1, 2)


def heuristic_trim(reaction_rel: float, dur: float) -> tuple[float, float]:
    """Start late without the judge: HEURISTIC_LEAD_S before the reaction, keeping at least the shortest
    allowed clip."""
    t0 = max(0.0, reaction_rel - HEURISTIC_LEAD_S)
    t0 = min(t0, max(0.0, dur - clip_bounds()[0]))
    return round(t0, 2), float(dur)
