"""Incremental D3 detector for live chat.

Same scoring as ``detect.detector.detect`` (0.5*z(U) + 0.35*z(E) + 0.15*z(S), trailing median/MAD baseline,
mute after raid / sub-bomb / giveaway, cooldown) but fed one message at a time. Times are epoch seconds.

A second is "final" once a message with a later second arrives (or ``tick(now_s)`` is called), so a candidate
fires about one second after the score crosses ``fire_z``.
"""

from __future__ import annotations

import re
from collections import Counter, deque

from clipwatcher import config
from clipwatcher.detect.detector import _FLOORS, _kw_patterns, _median, _norm, _Rec, _Robust, prompted_share
from clipwatcher.models import Candidate, ChatMessage

_SUB_BOMB_MIN = 5
_GIVEAWAY_LOOKAHEAD_S = 20


class LiveDetector:
    def __init__(
        self,
        streamer: str,
        viewers: int | None = None,
        *,
        delay_s: float | None = None,
        before_s: float = 20,
        after_s: float = 10,
    ):
        cfg = config.global_config().get("detector", {})
        self.streamer = streamer
        self.before_s, self.after_s = before_s, after_s
        self.w = cfg.get("weights", {"U": 0.5, "E": 0.35, "S": 0.15})
        self.win = int(cfg.get("window_s", 10))
        self.fire_z, self.pre_z = float(cfg.get("fire_z", 3.0)), float(cfg.get("pre_z", 2.0))
        sc0 = config.streamer(streamer)
        self.min_unique = int(sc0.get("min_unique", cfg.get("min_unique", 8)))
        self.min_unique_floor = int(cfg.get("min_unique_floor", 3))
        self._u_hist: deque[int] = deque(maxlen=int(cfg.get("baseline_min", 15)) * 60)
        self.trace: deque[tuple] | None = None  # set to a deque to record (t, score, u, u_need, hold, muted)
        self.viewers = viewers
        self.mute_s = float(cfg.get("mute_after_notice_s", 75))
        self.cooldown = float(cfg.get("cooldown_s", 45))
        sc = config.streamer(streamer)
        default_delay = sc.get("delay_s", cfg.get("default_delay_s", 8))
        self.delay_s = float(delay_s if delay_s is not None else default_delay)
        self.other_weight = float(cfg.get("other_weight", 0.5))
        self.prompt_max = float(cfg.get("prompt_share", 0.4))
        self._laugh = re.compile(cfg["laughter_regex"]) if cfg.get("laughter_regex") else None
        self._pats = _kw_patterns(config.keywords_for(streamer))
        self._wmap = {k: w for k, _, w in self._pats}
        hist_n = int(cfg.get("baseline_min", 15)) * 60
        self._hist_n = hist_n
        self._r = {k: _Robust(_FLOORS[k], hist_n) for k in "UES"}
        self._pct = float(cfg.get("min_unique_viewer_pct", 0.01))
        self._total = 0
        self._hits: Counter[str] = Counter()
        self._match_cache: dict[str, list[str]] = {}
        self._pending: dict[int, list[_Rec]] = {}
        self._recent: deque[tuple[int, list[_Rec]]] = deque()
        self._humans: deque[tuple[int, str, str]] = deque()  # (ts_ms, user, norm)
        self._spans: list[tuple[float, float]] = []
        self._bots_pending: list[int] = []  # bot message ts_ms awaiting giveaway evaluation
        self._gifts: deque[int] = deque()
        self._next: int | None = None
        self._start: int | None = None
        self._run_first: int | None = None
        self._last_fire = -1e9
        self._counts: deque[int] = deque(maxlen=hist_n)  # messages per window, for x_normal
        self._out: deque[Candidate] = deque()
        self._maturing: list[Candidate] = []
        self.mature_s = float(cfg.get("live_mature_s", 15))

    # -- public ---------------------------------------------------------------------------------
    def feed(self, msg: ChatMessage) -> Candidate | None:
        sec = msg.ts_ms // 1000
        if self._next is None:
            self._next = self._start = sec
        self._advance_to(sec)
        self._ingest(msg, sec)
        return self._out.popleft() if self._out else None

    def tick(self, now_s: float) -> Candidate | None:
        """Finalize seconds up to ``now_s`` without a message (quiet chat)."""
        if self._next is not None:
            self._advance_to(int(now_s))
        return self._out.popleft() if self._out else None

    def drain(self) -> list[Candidate]:
        out = list(self._out) + self._maturing
        self._out.clear()
        self._maturing = []
        return out

    # -- ingestion ------------------------------------------------------------------------------
    def _matches(self, text: str) -> list[str]:
        hit = self._match_cache.get(text)
        if hit is None:
            if len(self._match_cache) > 20000:
                self._match_cache.clear()
            hit = self._match_cache[text] = [k for k, p, _ in self._pats if p.search(text)]
        return hit

    def _rec_weight(self, text: str) -> float:
        keys = self._matches(text)
        for k in keys:
            self._hits[k] += 1
        cands = []
        for k in keys:
            rarity = max(1.0, (100.0 * self._hits[k] / max(1, self._total) / 5.0) ** 0.5)
            cands.append(self._wmap[k] / rarity)
        if self._laugh and self._laugh.search(text):
            cands.append(2.0)
        return max(cands) if cands else self.other_weight

    def _ingest(self, m: ChatMessage, sec: int) -> None:
        self._total += 1
        t = m.ts_ms / 1000
        if m.is_notice:
            if m.notice_type in ("raid", "submysterygift"):
                self._spans.append((t, t + self.mute_s))
            elif m.notice_type == "subgift":
                self._gifts.append(m.ts_ms)
                while self._gifts and m.ts_ms - self._gifts[0] > 10_000:
                    self._gifts.popleft()
                if len(self._gifts) >= _SUB_BOMB_MIN:
                    t0 = self._gifts[0] / 1000
                    self._spans.append((t0, t0 + self.mute_s))
                    self._gifts.clear()
            return
        if m.is_bot:
            self._bots_pending.append(m.ts_ms)
            return
        if m.text.startswith("!") or sec < (self._next or 0):
            return
        norm = _norm(m.text)
        rec = _Rec(m.user, self._rec_weight(m.text), len(m.text.split()) <= 3, norm)
        self._pending.setdefault(sec, []).append(rec)
        self._humans.append((m.ts_ms, m.user, norm))

    # -- stepping -------------------------------------------------------------------------------
    def _advance_to(self, sec: int) -> None:
        assert self._next is not None
        if sec - self._next > self._hist_n:
            self._next = sec - self._hist_n
        while self._next < sec:
            self._step(self._next)
            self._next += 1

    def _word_burst(self, b: float, until_ms: float) -> tuple[int, int]:
        """(chatters typing the most common message, all chatters) between bot message b and until_ms."""
        span = [h for h in self._humans if b < h[0] <= until_ms]
        users = {h[1] for h in span}
        top: Counter[str] = Counter()
        seen = set()
        for _, u, n in span:
            if n and (u, n) not in seen:
                seen.add((u, n))
                top[n] += 1
        return (top.most_common(1)[0][1] if top else 0), len(users)

    def _eval_giveaways(self, t: int) -> bool:
        """Hold fires only while a same-word burst is forming after a bot message.

        Big channels have bots posting every few seconds (timers, alerts), so holding after *every* bot
        message blocked nearly all fires; a replay of an 8 h VOD missed 4 of the top 10 moments that way.
        """
        hold = False
        keep = []
        for b in self._bots_pending:
            if t * 1000 < b + _GIVEAWAY_LOOKAHEAD_S * 1000:
                keep.append(b)
                same, n = self._word_burst(b, t * 1000)
                if same >= 3 and same >= 0.3 * max(n, 1):
                    hold = True
                continue
            same, n = self._word_burst(b, b + _GIVEAWAY_LOOKAHEAD_S * 1000)
            if n >= self.min_unique and same >= max(self.min_unique, 0.5 * n):
                self._spans.append((b / 1000, b / 1000 + self.mute_s))
        self._bots_pending = keep
        return hold

    def _step(self, t: int) -> None:
        recs = self._pending.pop(t, [])
        self._recent.append((t, recs))
        while self._recent and self._recent[0][0] <= t - self.win:
            self._recent.popleft()
        while self._humans and self._humans[0][0] < (t - 60) * 1000:
            self._humans.popleft()
        window = [r for _, rs in self._recent for r in rs]
        if window:
            best: dict[str, float] = {}
            for r in window:
                if r.weight > best.get(r.user, -1):
                    best[r.user] = r.weight
            counts = Counter(r.norm for r in window)
            u, e = len(best), sum(best.values())
            s = sum(1 for r in window if r.short or counts[r.norm] > 1) / len(window)
        else:
            u, e, s = 0, 0.0, 0.0
        hold = self._eval_giveaways(t)
        self._spans = [sp for sp in self._spans if sp[1] >= t - 5]
        rU, rE, rS = self._r["U"], self._r["E"], self._r["S"]
        score: float | None = None
        if self._start is not None and t - self._start >= 60:
            zu, ze, zs = rU.z(u), rE.z(e), rS.z(s)
            sc = self.w["U"] * zu + self.w["E"] * ze + self.w["S"] * zs
            if not any(a <= t <= b for a, b in self._spans) and not (
                window and prompted_share(window, self.other_weight) >= self.prompt_max
            ):
                score = sc
        if score is not None and score >= self.pre_z:
            if self._run_first is None:
                self._run_first = t
        else:
            self._run_first = None
        u_need = max(self.min_unique, self._pct * self.viewers if self.viewers else 0)
        # Small channels (most GTA RP chats): never ask for more unique chatters than the busiest 0.5% of
        # recent windows reached, as the batch detector does.
        if len(self._u_hist) >= 60:
            q = sorted(self._u_hist)[int(0.995 * (len(self._u_hist) - 1))]
            u_need = max(self.min_unique_floor, min(u_need, q))
        if self.trace is not None:
            muted = any(a <= t <= b for a, b in self._spans)
            self.trace.append((t, score, u, u_need, hold, muted, t - self._last_fire))
        if (
            score is not None
            and not hold
            and score >= self.fire_z
            and u >= u_need
            and t - self._last_fire >= self.cooldown
        ):
            first = self._run_first if self._run_first is not None else t
            self._last_fire = t
            peak = first - self.delay_s
            med = _median(list(self._counts)) if self._counts else 0.0
            self._maturing.append(
                Candidate(
                    stream=self.streamer,
                    peak_s=peak,
                    start_s=peak - self.before_s,
                    end_s=peak + self.after_s,
                    chat_score=score,
                    final_score=score,
                    x_normal=len(window) / max(1.0, med),
                    unique_chatters=u,
                    signals={"zU": rU.z(u), "zE": rE.z(e), "zS": rS.z(s), "fire_s": float(t)},
                )
            )
        # Let fired candidates mature: the score keeps climbing for seconds after the threshold is
        # crossed, so emit after mature_s with the peak score (the fast lane waits that long anyway).
        still = []
        for c in self._maturing:
            if score is not None and score > c.chat_score:
                c.chat_score = c.final_score = score
                c.unique_chatters = max(c.unique_chatters, u)
            if t - c.signals["fire_s"] >= self.mature_s:
                self._out.append(c)
            else:
                still.append(c)
        self._maturing = still
        self._counts.append(len(window))
        rU.push(u)
        self._u_hist.append(u)
        rE.push(e)
        rS.push(s)
