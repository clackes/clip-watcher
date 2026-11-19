"""Incremental D3/D13 detector for live chat.

Same scoring as ``detect.detector.detect`` (weighted z of chatters, messages, keyword energy and short share,
mute after raid / sub-bomb / giveaway) but fed one message at a time. Times are epoch seconds.

The baseline is the whole stream so far (``SessionTable``), not a rolling window: ``prime()`` loads the chat
from before the bot joined (replay + saved log) so the first live spike is already measured against hours of
history, and a restart mid-stream picks up where it left off.

A second is "final" once a message with a later second arrives (or ``tick(now_s)`` is called). A spike that
crosses the fire threshold matures for ``live_mature_s`` and is emitted only if its peak reaches the keep
threshold; a weak fire starts no cooldown, so it cannot shadow the real spike right behind it.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Iterable

from clipwatcher import config
from clipwatcher.detect.detector import (
    _SUB_BOMB_MIN,
    CLIP_LINK,
    _kw_patterns,
    _norm,
    _Rec,
    gate,
    mystery_bomb,
    new_table,
    prompted_share,
    settings,
    window_features,
)
from clipwatcher.detect.session import Row
from clipwatcher.models import Candidate, ChatMessage

_GIVEAWAY_LOOKAHEAD_S = 20
_GAP_MAX_S = 900  # a longer hole in the chat (bot offline, no replay) is skipped, not filled with silence


class LiveDetector:
    def __init__(
        self,
        streamer: str,
        viewers: int | None = None,
        *,
        delay_s: float | None = None,
        before_s: float | None = None,
        after_s: float | None = None,
    ):
        p = self.p = settings(streamer)
        self.streamer = streamer
        self.before_s = p["before_s"] if before_s is None else before_s
        self.after_s = p["after_s"] if after_s is None else after_s
        self.win = p["win"]
        self.min_unique, self.min_unique_floor = p["min_unique"], p["min_unique_floor"]
        self.trace: deque[tuple] | None = None  # set to a deque to record (t, score, u, u_need, hold, muted)
        self.viewers = viewers
        self.mute_s = p["mute_s"]
        # two kept spikes closer than a clip is long would be the same footage twice
        self.cooldown = max(p["cooldown"], self.before_s + self.after_s)
        self.mature_s = p["mature_s"]
        self.delay_s = float(delay_s if delay_s is not None else p["delay_s"])
        self.other_weight = p["other_weight"]
        self.prompt_max = p["prompt_share"]
        self._laugh = re.compile(p["laughter"]) if p["laughter"] else None
        self._pats = _kw_patterns(config.keywords_for(streamer))
        self._wmap = {k: w for k, _, w in self._pats}
        self.table = new_table(p)
        self.spikes: list[tuple[float, Row, Row]] = []  # kept: (fire_s, peak row, last-hour medians then)
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
        self._last_keep, self._last_keep_score = -1e9, 0.0
        self._kept: deque[tuple[float, float]] = deque()  # (fire_s, score) of the last hour's kept spikes
        self._live_from = -1e9  # spikes fired up to this second are history: recorded, not emitted
        self._out: deque[Candidate] = deque()
        self._maturing: list[tuple[Candidate, Row, Row]] = []  # with the peak row and its medians

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
        _, keep = gate(self.table, self.p)
        out = list(self._out) + [c for c, *_ in self._maturing if c.chat_score >= keep and self._is_live(c)]
        self._out.clear()
        self._maturing = []
        return out

    def prime(self, history: Iterable[ChatMessage]) -> int:
        """Load the chat from before now (oldest first) as baseline. Nothing in it is emitted as a clip:
        the video of those moments is gone or was already handled. Returns the number of messages read."""
        self.table.auto = False  # one refresh at the end instead of one every few rows
        n, last = 0, None
        for msg in history:
            self.feed(msg)
            n, last = n + 1, msg
        self.table.auto = True
        if last is not None:
            self._live_from = last.ts_ms / 1000
            self.table.refresh()
            self._history_spikes()
        self._out.clear()
        return n

    def _history_spikes(self) -> None:
        """What this stream's spikes looked like before the bot joined, scored against the whole of it:
        the next live spike is ranked among them (signals.session_rank)."""
        tab = self.table
        fire, keep = gate(tab, self.p)
        span, i, last = int(self.mature_s), 0, -1e9
        while i < len(tab):
            if not tab.scorable[i] or i - last < self.cooldown or tab.score(*tab.row(i))[0] < fire:
                i += 1
                continue
            best = max(
                (j for j in range(i, min(len(tab), i + span + 1)) if tab.scorable[j]),
                key=lambda j: tab.score(*tab.row(j))[0],
            )
            if tab.score(*tab.row(best))[0] >= keep:
                self.spikes.append((float(tab.secs[i]), *tab.row(best)))
                last = i
            i += span + 1

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
        if CLIP_LINK.search(text):
            cands.append(3.0)
        return max(cands) if cands else self.other_weight

    def _ingest(self, m: ChatMessage, sec: int) -> None:
        self._total += 1
        t = m.ts_ms / 1000
        if m.is_notice:
            if m.notice_type == "raid" or mystery_bomb(m):
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
        if sec - self._next > _GAP_MAX_S:
            self._next = sec - _GAP_MAX_S
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

    def _is_live(self, c: Candidate) -> bool:
        return c.signals["fire_s"] > self._live_from

    def _over_budget(self, c: Candidate) -> bool:
        """The gate keeps the top seconds of a stream, and a flat chat whose top seconds are scattered noise
        would turn each into a clip. Past ``max_per_hour`` a spike must beat the hour's typical kept one."""
        while self._kept and self._kept[0][0] < c.signals["fire_s"] - 3600:
            self._kept.popleft()
        if len(self._kept) < self.p["max_per_hour"]:
            return False
        scores = sorted(s for _, s in self._kept)
        return c.chat_score <= scores[len(scores) // 2]

    def _onset(self, t: int) -> int:
        """Where the burst peaking at ``t`` began: the start of the run above the pre-threshold, but no
        further back than a burst takes to build (a long lively stretch is one run for minutes)."""
        first = self._run_first if self._run_first is not None else t
        return max(first, t - int(self.p["onset_max_s"]))

    def _rank(self, row: Row, recent: Row) -> int:
        """Place of a spike among the kept spikes of this stream, all scored against today's baseline."""
        score = self.table.score(row, recent)[0]
        return 1 + sum(1 for _, r, lv in self.spikes if self.table.score(r, lv)[0] > score)

    def _step(self, t: int) -> None:
        recs = self._pending.pop(t, [])
        self._recent.append((t, recs))
        while self._recent and self._recent[0][0] <= t - self.win:
            self._recent.popleft()
        while self._humans and self._humans[0][0] < (t - 60) * 1000:
            self._humans.popleft()
        window = [r for _, rs in self._recent for r in rs]
        row = window_features(window)
        u = row[0]
        hold = self._eval_giveaways(t)
        self._spans = [sp for sp in self._spans if sp[1] >= t - 5]
        tab = self.table
        muted = any(a <= t <= b for a, b in self._spans) or (
            bool(window) and prompted_share(window, self.other_weight) >= self.prompt_max
        )
        warm = self._start is not None and t - self._start >= 60
        primed = tab.auto  # False while history is loading: rows only, nothing is scored against a half table
        score: float | None = None
        zs: dict[str, float] = {}
        fire_z, keep_z = gate(tab, self.p) if primed else (float("inf"), float("inf"))
        recent = tab.recent()
        if warm and not muted and primed:
            score, zs = tab.score(row, recent)
        if score is not None and score >= fire_z * self.p["pre_z"] / self.p["fire_z"]:
            if self._run_first is None:
                self._run_first = t
        else:
            self._run_first = None
        u_need = max(self.min_unique, self.p["pct"] * self.viewers if self.viewers else 0)
        # Small channels (most GTA RP chats): never ask for more unique chatters than the busiest 0.5% of
        # windows of this stream reached, as the batch detector does.
        if len(tab) >= 60:
            u_need = max(self.min_unique_floor, min(u_need, tab.u_top))
        if self.trace is not None:
            self.trace.append((t, score, u, u_need, hold, muted, t - self._last_keep))
        if (
            score is not None
            and not hold
            and not self._maturing
            and score >= fire_z
            and u >= u_need
            # a spike half as big again as the one just kept is another moment: no waiting out the cooldown
            and (t - self._last_keep >= self.cooldown or score >= 1.5 * self._last_keep_score)
        ):
            peak = self._onset(t) - self.delay_s
            cand = Candidate(
                stream=self.streamer,
                peak_s=peak,
                start_s=peak - self.before_s,
                end_s=peak + self.after_s,
                chat_score=score,
                final_score=score,
                x_normal=row[1] / max(1.0, tab.med["M"]),
                unique_chatters=u,
                signals={**{f"z{k}": v for k, v in zs.items()}, "fire_s": float(t), "gate": keep_z},
            )
            self._maturing.append((cand, row, recent))
        # Let fired candidates mature: the score keeps climbing for seconds after the threshold is
        # crossed, so decide after mature_s on the peak score (the fast lane waits that long anyway).
        still = []
        for c, peak_row, peak_recent in self._maturing:
            if score is not None and score > c.chat_score:
                c.chat_score = c.final_score = score
                c.unique_chatters = max(c.unique_chatters, u)
                c.x_normal = row[1] / max(1.0, tab.med["M"])
                c.signals.update({f"z{k}": v for k, v in zs.items()})
                c.peak_s = self._onset(t) - self.delay_s
                c.start_s, c.end_s = c.peak_s - self.before_s, c.peak_s + self.after_s
                peak_row, peak_recent = row, recent
            if t - c.signals["fire_s"] < self.mature_s:
                still.append((c, peak_row, peak_recent))
            elif c.chat_score >= keep_z and not self._over_budget(c):
                self._last_keep, self._last_keep_score = c.signals["fire_s"], c.chat_score
                self._kept.append((c.signals["fire_s"], c.chat_score))
                c.signals["session_pct"] = tab.share_below(c.chat_score)
                c.signals["session_rank"] = float(self._rank(peak_row, peak_recent))
                self.spikes.append((c.signals["fire_s"], peak_row, peak_recent))
                if self._is_live(c):
                    self._out.append(c)
        self._maturing = still
        tab.push(*row, scorable=warm and not muted, sec=t)
