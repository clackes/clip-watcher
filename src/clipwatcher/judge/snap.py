"""Snap judge trim points to word and sentence boundaries so clips never start or end mid-word.

Words come from Whisper with punctuation attached, times relative to the candidate window.
No extra audio pass: the live lane cannot afford one, and word gaps already mark the pauses.
"""

from __future__ import annotations

from clipwatcher.models import Word

LEAD_S = 0.15  # silence kept before the first word
TAIL_S = 0.35  # silence kept after the last word
START_BACK_S = 1.5  # move the start back to a sentence start at most this far
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

    # Start: first word that is still being spoken at t0 or comes after it.
    i = next((k for k, w in enumerate(ws) if w.end > s), None)
    if i is not None:
        # A sentence that begins shortly before t0 is a better entry point than its middle.
        for k in range(i, 0, -1):
            if s - ws[k].start > START_BACK_S:
                break
            if _ends_sentence(ws[k - 1]):
                i = k
                break
        else:  # no sentence end in reach: the transcript's first word starts the sentence
            if s - ws[0].start <= START_BACK_S:
                i = 0
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
