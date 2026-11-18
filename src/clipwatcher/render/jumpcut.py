"""Jump cuts: drop the dead air between lines, the way clip channels edit (owner, 18 Nov).

Every top clip channel we studied (dailyspeedzone, speeduniverse, deagzzzshorts, JZBNShorts, Zylok.clipss)
cuts the pauses between lines so something is said or happens every second. A pause is cut when nobody
speaks for longer than render.jump_gap_s, unless something the judge marked happens in it (a beat or an
action label: silent fails and faces are the moment, not dead air). Clips with almost no speech are left
alone. All times are seconds relative to the clip start.
"""

from __future__ import annotations

from clipwatcher import config
from clipwatcher.models import Word

PRE_S = 0.15  # kept before the next line starts
POST_S = 0.3  # kept after a line ends (the last syllable and a beat of reaction)
MIN_WORDS = 6  # fewer spoken words than this: a visual clip, no cuts
PROTECT_S = 1.2  # around a beat nothing is cut
MAX_GAP_S = 4.0  # a longer silence is the content (watching, a stare, a silent fail), not dead air
MIN_LEN_S, MIN_SHARE = 12.0, 0.6  # cuts never take the clip below this length or share of its length


def settings() -> tuple[bool, float]:
    r = config.global_config().get("render", {})
    return bool(r.get("jump_cuts", True)), float(r.get("jump_gap_s", 0.8))


def keep_ranges(
    words: list[Word], dur: float, *, protect: list[tuple[float, float]] = (), gap: float | None = None
) -> list[tuple[float, float]]:
    """[(start, end)] of the clip to keep, in order. [(0, dur)] when nothing is cut."""
    on, g = settings()
    gap = g if gap is None else gap
    ws = sorted((w for w in words if w.text.strip() and w.end > 0 and w.start < dur), key=lambda w: w.start)
    if not on or len(ws) < MIN_WORDS:
        return [(0.0, dur)]
    # silent stretches: before the first word, between lines, after the last word
    edges = [(0.0, ws[0].start)] + [(a.end, b.start) for a, b in zip(ws, ws[1:], strict=False)]
    edges.append((ws[-1].end, dur))
    cuts = []
    for a, b in edges:
        lo = a + POST_S if a > 0 else 0.0
        hi = b - PRE_S if b < dur else dur
        if b - a < gap or hi - lo < 0.3:
            continue
        if any(lo < pb and pa < hi for pa, pb in protect):
            continue
        if a == 0.0 or b == dur:  # leading/trailing silence: only trim it, the judge chose the edges
            continue
        if b - a > MAX_GAP_S:
            continue
        cuts.append((lo, hi))
    floor = min(dur, max(MIN_LEN_S, MIN_SHARE * dur))
    budget = dur - floor  # seconds that may go; shortest pauses first (the dead air between lines)
    kept_cuts = []
    for lo, hi in sorted(cuts, key=lambda c: c[1] - c[0]):
        if hi - lo <= budget:
            kept_cuts.append((lo, hi))
            budget -= hi - lo
    cuts = sorted(kept_cuts)
    keep, t = [], 0.0
    for lo, hi in cuts:
        if lo > t:
            keep.append((round(t, 3), round(lo, 3)))
        t = hi
    if dur > t:
        keep.append((round(t, 3), round(dur, 3)))
    return keep or [(0.0, dur)]


def remap(t: float, keep: list[tuple[float, float]]) -> float | None:
    """Time on the cut timeline; None when t falls in a removed stretch."""
    out = 0.0
    for a, b in keep:
        if a <= t <= b:
            return round(out + t - a, 3)
        out += b - a
    return None


def remap_words(words: list[Word], keep: list[tuple[float, float]]) -> list[Word]:
    out = []
    for w in words:
        s, e = remap(w.start, keep), remap(w.end, keep)
        if s is None and e is None:
            continue
        s = e - 0.05 if s is None else s
        e = s + 0.05 if e is None else e
        out.append(Word(s, max(e, s + 0.05), w.text))
    return out


def length(keep: list[tuple[float, float]]) -> float:
    return round(sum(b - a for a, b in keep), 3)
