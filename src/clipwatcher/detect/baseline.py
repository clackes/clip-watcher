"""Chat Clip Finder style baseline: messages per bucket vs the mean of the surrounding 5 minutes."""

from __future__ import annotations

from collections.abc import Sequence

from clipwatcher.models import Candidate, ChatMessage


def x_normal_series(
    messages: Sequence[ChatMessage], bucket_s: int = 10, surround_s: int = 300
) -> list[tuple[float, int, float]]:
    """Return [(bucket_start_s, count, x_normal)]. Surround = +-surround_s/2 excluding the bucket."""
    if not messages:
        return []
    n = int(max(m.ts_ms for m in messages) // 1000 // bucket_s) + 1
    counts = [0] * n
    for m in messages:
        counts[int(m.ts_ms // 1000 // bucket_s)] += 1
    half = max(1, int(surround_s / 2 // bucket_s))
    pre = [0]
    for c in counts:
        pre.append(pre[-1] + c)
    out = []
    for i, c in enumerate(counts):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        cnt = hi - lo - 1
        mean = (pre[hi] - pre[lo] - c) / cnt if cnt > 0 else 0.0
        out.append((float(i * bucket_s), c, c / max(mean, 0.5)))
    return out


def x_normal_at(series: Sequence[tuple[float, int, float]], t_s: float, bucket_s: int = 10) -> float:
    if not series:
        return 0.0
    i = min(max(int(t_s // bucket_s), 0), len(series) - 1)
    return series[i][2]


def x_normal_top(
    messages: Sequence[ChatMessage],
    top: int = 5,
    before: float = 20,
    after: float = 10,
    gap: float = 90,
    bucket_s: int = 10,
    stream: str = "baseline",
) -> list[Candidate]:
    series = x_normal_series(messages, bucket_s)
    kept: list[Candidate] = []
    for start, _count, x in sorted(series, key=lambda s: -s[2]):
        peak = start + bucket_s / 2
        if any(abs(peak - k.peak_s) < gap for k in kept):
            continue
        kept.append(
            Candidate(
                stream=stream,
                peak_s=peak,
                start_s=max(0.0, peak - before),
                end_s=peak + after,
                chat_score=x,
                final_score=x,
                x_normal=x,
            )
        )
        if len(kept) >= top:
            break
    return kept
