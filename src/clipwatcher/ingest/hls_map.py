"""Map recording offsets (seconds) to wall-clock epoch ms via program-date-time anchors."""

from __future__ import annotations

import bisect


class PdtMap:
    """Piecewise-linear map built from (segment_index, pdt_ms) anchors.

    Offset of a segment is index * segment_s. Between two anchors whose wall-clock delta matches
    the offset delta (within tol_s) the map interpolates linearly; otherwise the span is a gap
    (ad break, restart): the earlier anchor runs at 1 s/s up to the next anchor and epochs inside
    the hole between that run-out and the next anchor map to None.
    """

    def __init__(
        self, pairs: list[tuple[int, int]] | None = None, segment_s: float = 10.0, tol_s: float = 1.0
    ):
        self.segment_s = segment_s
        self.tol_ms = tol_s * 1000
        self._pts: list[tuple[float, int]] = []  # (offset_s, epoch_ms)
        for idx, pdt in pairs or []:
            self.add(idx, pdt)

    def add(self, index: int, pdt_ms: int) -> None:
        bisect.insort(self._pts, (index * self.segment_s, int(pdt_ms)))

    def _is_gap(self, a: tuple[float, int], b: tuple[float, int]) -> bool:
        return abs((b[1] - a[1]) - (b[0] - a[0]) * 1000) > self.tol_ms

    def gaps(self) -> list[tuple[int, int]]:
        """Wall-clock (start_ms, end_ms) holes."""
        out = []
        for a, b in zip(self._pts, self._pts[1:], strict=False):
            if self._is_gap(a, b):
                out.append((int(a[1] + (b[0] - a[0]) * 1000), b[1]))
        return out

    def epoch_ms(self, offset_s: float) -> float:
        if not self._pts:
            raise ValueError("empty map")
        offs = [p[0] for p in self._pts]
        i = bisect.bisect_right(offs, offset_s) - 1
        if i < 0:
            o, e = self._pts[0]
            return e + (offset_s - o) * 1000
        a = self._pts[i]
        if i + 1 < len(self._pts):
            b = self._pts[i + 1]
            if not self._is_gap(a, b):
                f = (offset_s - a[0]) / (b[0] - a[0]) if b[0] != a[0] else 0.0
                return a[1] + f * (b[1] - a[1])
        return a[1] + (offset_s - a[0]) * 1000

    def offset_s(self, epoch_ms: float) -> float | None:
        """Inverse map; None when epoch_ms falls inside a gap."""
        if not self._pts:
            raise ValueError("empty map")
        for s, e in self.gaps():
            if s < epoch_ms < e:
                return None
        eps = [p[1] for p in self._pts]
        i = bisect.bisect_right(eps, epoch_ms) - 1
        if i < 0:
            o, e = self._pts[0]
            return o + (epoch_ms - e) / 1000
        a = self._pts[i]
        if i + 1 < len(self._pts):
            b = self._pts[i + 1]
            if not self._is_gap(a, b) and b[1] != a[1]:
                return a[0] + (epoch_ms - a[1]) / (b[1] - a[1]) * (b[0] - a[0])
        return a[0] + (epoch_ms - a[1]) / 1000
