"""The stream so far as a table: one row of chat features per second since the broadcast started.

The spike baseline and the gate are statistics of this table, so both reflect the whole stream instead of the
last few minutes (D13). Rows come from the replayed chat of before the bot joined, the saved chat log of
earlier runs and the live chat, in that order.

What each part of a score is measured against:
- level ("what is normal right now"): half the median of the whole stream, half the median of the last hour.
  The whole-stream half keeps a lively stretch from becoming its own baseline; the last-hour half follows a
  chat that doubles after a raid or thins out overnight.
- scale ("how much does this chat wobble"): spread of every second of the stream around its level.
- gate ("how big are this stream's spikes"): quantiles of the scores of every second of the stream.

Why not the old trailing 15 minutes for all three: replayed against 180 viewer-made clips from six real
streams it scored moments inside a lively stretch too low and noise inside a calm one too high, and its fixed
score gate produced nothing at all on the busiest chat.
"""

from __future__ import annotations

from array import array
from bisect import bisect_left, insort
from collections import deque
from collections.abc import Mapping

MAD_K = 1.4826
KEYS = ("U", "M", "E", "S")  # unique chatters, messages, keyword energy, short/repeated share
Row = tuple[float, float, float, float]


def _median(s: list[float]) -> float:
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


class _RollingMedian:
    """Median of the last ``size`` values, exact, a few microseconds per value."""

    def __init__(self, size: int):
        self.size = size
        self._order: deque[float] = deque()
        self._sorted: list[float] = []

    def push(self, x: float) -> None:
        self._order.append(x)
        insort(self._sorted, x)
        if len(self._order) > self.size:
            del self._sorted[bisect_left(self._sorted, self._order.popleft())]

    @property
    def value(self) -> float:
        return _median(self._sorted) if self._sorted else 0.0


class SessionTable:
    """Per-second (U, M, E, S) rows, the level each was measured against, and the statistics over them."""

    def __init__(
        self,
        floors: Mapping[str, float],
        weights: Mapping[str, float],
        *,
        level_s: int = 3600,
        session_share: float = 0.5,
        max_rows: int = 86400,
    ):
        self.floors = {k: float(floors[k]) for k in KEYS}
        self.w = {k: float(weights.get(k, 0.0)) for k in KEYS}
        # weight of the whole-stream median in the level; all of it when there is no recent window
        self.share = min(1.0, max(0.0, session_share)) if level_s else 1.0
        self.max_rows = int(max_rows)
        self.cols = {k: array("f") for k in KEYS}
        self.lvls = {k: array("f") for k in KEYS}  # last-hour median when the row was live
        self.secs = array("I")  # the second each row describes (epoch live, VOD offset in batch)
        self.scorable = bytearray()  # 1 = the second could have fired (past warm-up, not muted)
        self.med = dict.fromkeys(KEYS, 0.0)  # whole-stream medians
        self.sd = dict(self.floors)
        self.u_top = 0.0  # 99.5th percentile of unique chatters per window
        self._recent = {k: _RollingMedian(int(level_s) or 1) for k in KEYS}
        self._scores: list[float] = []  # sorted scores of the scorable rows under the current baseline
        self._at = 0  # rows when the statistics were last refreshed
        self.auto = True  # False while loading history in bulk: one refresh() at the end is enough

    def __len__(self) -> int:
        return len(self.scorable)

    @property
    def scored(self) -> int:
        """Rows behind the gate quantiles."""
        return len(self._scores)

    # -- rows ---------------------------------------------------------------------------------------
    def recent(self) -> Row:
        """Last-hour median of each column, as of the rows pushed so far."""
        r = self._recent
        return r["U"].value, r["M"].value, r["E"].value, r["S"].value

    def push(self, u: float, m: float, e: float, s: float, scorable: bool = True, sec: int = 0) -> None:
        for k, v, lv in zip(KEYS, (u, m, e, s), self.recent(), strict=True):
            self.cols[k].append(v)
            self.lvls[k].append(lv)
            self._recent[k].push(v)
        self.secs.append(sec)
        self.scorable.append(1 if scorable else 0)
        n = len(self.scorable)
        if n > self.max_rows + 600:  # a marathon: the statistics cover the last max_rows seconds
            self._drop(n - self.max_rows)
            n = len(self.scorable)
        # every row while tiny, every 5 s in the first hour, then about every 0.15% of the stream
        every = 1 if n < 30 else 5 if n < 3600 else min(60, n // 720)
        if self.auto and n - self._at >= every:
            self.refresh()

    def _drop(self, k: int) -> None:
        for arr in (*self.cols.values(), *self.lvls.values(), self.secs, self.scorable):
            del arr[:k]
        self._at = max(0, self._at - k)

    def refresh(self) -> None:
        n = len(self.scorable)
        if not n:
            return
        a, c, mix = {}, 0.0, 1.0 - self.share
        for k in KEYS:
            col = sorted(self.cols[k])
            med = self.med[k] = _median(col)
            if k == "U":
                self.u_top = col[int(0.995 * (n - 1))]
            # spread around the level; the level's whole-stream half is one constant and drops out of it
            lv = self.lvls[k]
            res = sorted(x - mix * lv[i] for i, x in enumerate(self.cols[k]))
            mid = _median(res)
            self.sd[k] = max(MAD_K * _median(sorted(abs(r - mid) for r in res)), self.floors[k])
            a[k] = self.w[k] / self.sd[k]
            c += a[k] * self.share * med
        cu, cm, ce, cs = (self.cols[k] for k in KEYS)
        lu, lm, le, ls = (self.lvls[k] for k in KEYS)
        au, am, ae, as_, ok = a["U"], a["M"], a["E"], a["S"], self.scorable
        self._scores = sorted(
            au * (cu[i] - mix * lu[i]) + am * (cm[i] - mix * lm[i]) + ae * (ce[i] - mix * le[i])
            + as_ * (cs[i] - mix * ls[i]) - c
            for i in range(n)
            if ok[i]
        )  # fmt: skip
        self._at = n

    # -- statistics ---------------------------------------------------------------------------------
    def score(self, row: Row, recent: Row | None = None) -> tuple[float, dict[str, float]]:
        """Score of one second and its z per column. ``recent`` = the last-hour medians it was live against
        (default: now)."""
        recent = recent or self.recent()
        zs = {}
        for k, x, lv in zip(KEYS, row, recent, strict=True):
            level = self.share * self.med[k] + (1.0 - self.share) * lv
            zs[k] = (x - level) / self.sd[k]
        return sum(self.w[k] * zs[k] for k in KEYS), zs

    def row(self, i: int) -> tuple[Row, Row]:
        """(features, last-hour medians) of row ``i``."""
        c, lv = self.cols, self.lvls
        return (c["U"][i], c["M"][i], c["E"][i], c["S"][i]), (lv["U"][i], lv["M"][i], lv["E"][i], lv["S"][i])

    def quantile(self, q: float) -> float | None:
        """Score that share ``q`` of the stream's scorable seconds stay below."""
        if not self._scores:
            return None
        return self._scores[min(len(self._scores) - 1, int(q * len(self._scores)))]

    def share_below(self, score: float) -> float:
        """How unusual a score is for this stream: the share of its seconds that scored lower."""
        if not self._scores:
            return 0.0
        return bisect_left(self._scores, score) / len(self._scores)
