"""D3 spike detector: chat_score = 0.5*z(U) + 0.35*z(E) + 0.15*z(S), median/MAD over a trailing window."""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace

from clipwatcher import config
from clipwatcher.detect.baseline import x_normal_at, x_normal_series
from clipwatcher.models import Candidate, ChatMessage

_MAD_K = 1.4826
_FLOORS = {"U": 1.5, "E": 2.0, "S": 0.08}
_SUB_BOMB_TYPES = {"submysterygift", "subgift"}


@dataclass
class _Rec:
    user: str
    weight: float
    short: bool
    norm: str
    react: bool = False  # a real reaction: strong keyword (weight >= 1.5), laughter or a pasted clip link


REACT_MIN_WEIGHT = 1.5  # keywords at or above this weight are a reaction (lol, LMAO, KEKW, omg, wtf, ????)


def reaction_share(window: Sequence[_Rec]) -> float:
    """Share of messages that are a real reaction. Chat spikes made of game- or streamer-prompted words
    ("TIP TIP" in a strip club, "shoe store", "missions", emote spam, yes/no polls) sit at 0-0.10 on the
    18 Nov GTA VI live chats; laughing moments at 0.25-0.60 (owner OK'd the gate)."""
    return sum(1 for r in window if r.react) / len(window) if window else 0.0


CLIP_LINK = re.compile(r"(?i)(clips\.twitch\.tv/|twitch\.tv/\w+/clip/|kick\.com/\w+/clips?/)")


def prompted_share(window: Sequence[_Rec], other_weight: float) -> float:
    """Share of chatters repeating keyword-less messages that 3+ chatters typed (copy-paste).

    Streamer-prompted copy-paste ("type IWASHERE", "1 if you ...") looks like a spike but is not a moment;
    reaction spam (KEKW, LMAO, skulls) has keyword weight and is not counted here.
    """
    users = {r.user for r in window}
    if not users:
        return 0.0
    plain: dict[str, set[str]] = {}
    for r in window:
        if r.norm and r.weight <= other_weight:
            plain.setdefault(r.norm, set()).add(r.user)
    return sum(len(v) for v in plain.values() if len(v) >= 3) / len(users)


def _norm(text: str) -> str:
    t = re.sub(r"[^\w\s]", "", text.lower())
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)
    return " ".join(t.split())


def _kw_patterns(keywords: dict[str, float]) -> list[tuple[str, re.Pattern[str], float]]:
    return [
        (k, re.compile(rf"(?<!\w){re.escape(k)}(?!\w)", re.IGNORECASE), float(w)) for k, w in keywords.items()
    ]


def _build_records(
    msgs: Sequence[ChatMessage], keywords: dict[str, float], other_weight: float, laughter: str
) -> list[tuple[int, _Rec]]:
    pats = _kw_patterns(keywords)
    laugh = re.compile(laughter) if laughter else None
    # Channel familiarity: keywords already in many messages are less surprising.
    hits: Counter[str] = Counter()
    cache: dict[str, list[str]] = {}
    for m in msgs:
        if m.text not in cache:
            cache[m.text] = [k for k, p, _ in pats if p.search(m.text)]
        for k in cache[m.text]:
            hits[k] += 1
    total = max(1, len(msgs))
    rarity = {k: max(1.0, math.sqrt(100.0 * c / total / 5.0)) for k, c in hits.items()}
    wmap = {k: w for k, _, w in pats}
    wcache: dict[str, float] = {}
    rcache: dict[str, bool] = {}
    out = []
    for m in msgs:
        if m.is_bot or m.is_notice or m.text.startswith("!"):
            continue
        if m.text not in wcache:
            cands = [wmap[k] / rarity[k] for k in cache[m.text]]
            if laugh and laugh.search(m.text):
                cands.append(2.0)
            if CLIP_LINK.search(m.text):  # a viewer pasting a clip of it: strongest chat signal
                cands.append(3.0)
            wcache[m.text] = max(cands) if cands else other_weight
            rcache[m.text] = (
                any(wmap[k] >= REACT_MIN_WEIGHT for k in cache[m.text])
                or bool(laugh and laugh.search(m.text))
                or bool(CLIP_LINK.search(m.text))
            )
        toks = m.text.split()
        rec = _Rec(m.user, wcache[m.text], len(toks) <= 3, _norm(m.text), rcache[m.text])
        out.append((m.ts_ms // 1000, rec))
    return out


def _mute_spans(
    msgs: Sequence[ChatMessage], mute_s: float, min_unique: int, sub_bomb_min: int = 10
) -> list[tuple[float, float]]:
    """Raid / sub-bomb notices and giveaway bursts (bot message then one word typed by most chatters).

    Only a real sub-bomb (>= sub_bomb_min gifts) mutes: on busy days 1-5 sub gifts arrive every few seconds
    and muting on each kept xqc's detector deaf 99% of the GTA VI launch (18 Nov)."""
    spans: list[tuple[float, float]] = []
    notices = [m for m in msgs if m.is_notice]
    for m in notices:
        t = m.ts_ms / 1000
        recent = [n for n in notices if 0 <= (n.ts_ms / 1000 - t) <= 10]
        bomb = m.notice_type in _SUB_BOMB_TYPES and len(recent) >= sub_bomb_min and recent[0] is m
        mystery = m.notice_type == "submysterygift" and (m.viewers is None or m.viewers >= sub_bomb_min)
        if m.notice_type == "raid" or mystery or bomb:
            spans.append((t, t + mute_s))
    humans = [m for m in msgs if not (m.is_bot or m.is_notice)]
    times = [m.ts_ms for m in humans]
    for b in msgs:
        if not b.is_bot or b.is_notice:
            continue
        lo = bisect.bisect_right(times, b.ts_ms)
        hi = bisect.bisect_right(times, b.ts_ms + 20_000)
        span = humans[lo:hi]
        users = {m.user for m in span}
        if len(users) < min_unique:
            continue
        top = Counter()
        seen = set()
        for m in span:
            k = (m.user, _norm(m.text))
            if k not in seen and k[1]:
                seen.add(k)
                top[k[1]] += 1
        if top and top.most_common(1)[0][1] >= max(min_unique, 0.5 * len(users)):
            spans.append((b.ts_ms / 1000, b.ts_ms / 1000 + mute_s))
    return spans


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


class _Robust:
    """Trailing median/MAD, refreshed every few steps."""

    def __init__(self, floor: float, size: int, refresh: int = 5):
        self.hist: list[float] = []
        self.floor, self.size, self.refresh = floor, size, refresh
        self.med, self.sd, self._n = 0.0, floor, 0

    def z(self, x: float) -> float:
        return (x - self.med) / self.sd

    def push(self, x: float) -> None:
        self.hist.append(x)
        if len(self.hist) > self.size:
            self.hist.pop(0)
        self._n += 1
        if self._n % self.refresh == 1 or len(self.hist) < 30:
            self.med = _median(self.hist)
            mad = _median([abs(v - self.med) for v in self.hist])
            self.sd = max(_MAD_K * mad, self.floor)


def detect(
    messages: Sequence[ChatMessage],
    *,
    streamer: str,
    viewers: int | None = None,
    top: int = 5,
    before_s: float = 20,
    after_s: float = 10,
    gap_s: float = 90,
    delay_s: float | None = None,
    bucket_s: int = 10,
) -> list[Candidate]:
    cfg = config.global_config().get("detector", {})
    sc = config.streamer(streamer)
    w = cfg.get("weights", {"U": 0.5, "E": 0.35, "S": 0.15})
    win = int(cfg.get("window_s", 10))
    fire_z, pre_z = float(cfg.get("fire_z", 3.0)), float(cfg.get("pre_z", 2.0))
    min_unique = int(sc.get("min_unique", cfg.get("min_unique", 8)))
    pct = float(cfg.get("min_unique_viewer_pct", 0.01))
    mute_s = float(cfg.get("mute_after_notice_s", 75))
    cooldown = float(cfg.get("cooldown_s", 45))
    if delay_s is None:
        delay_s = float(sc.get("delay_s", cfg.get("default_delay_s", 8)))
    hist_n = int(cfg.get("baseline_min", 15)) * 60
    other_w = float(cfg.get("other_weight", 0.5))
    prompt_max = float(cfg.get("prompt_share", 0.4))
    u_need = max(min_unique, pct * viewers if viewers else 0)

    msgs = sorted(messages, key=lambda m: m.ts_ms)
    if not msgs:
        return []
    recs = _build_records(
        msgs,
        config.keywords_for(streamer),
        float(cfg.get("other_weight", 0.5)),
        cfg.get("laughter_regex", ""),
    )
    spans = _mute_spans(msgs, mute_s, min_unique, int(cfg.get("sub_bomb_min", 10)))
    horizon = int(msgs[-1].ts_ms // 1000) + 1
    per_sec: list[list[_Rec]] = [[] for _ in range(horizon + 1)]
    for sec, r in recs:
        per_sec[sec].append(r)

    rU, rE, rS = (_Robust(_FLOORS[k], hist_n) for k in "UES")
    min_react = float(cfg.get("min_reaction_share", 0.0))
    steps: list[tuple[float, float, float, float, int, float] | None] = []
    for t in range(horizon + 1):
        window = [r for s in range(max(0, t - win + 1), t + 1) for r in per_sec[s]]
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
        if t >= 60:
            zu, ze, zs = rU.z(u), rE.z(e), rS.z(s)
            score = w["U"] * zu + w["E"] * ze + w["S"] * zs
            muted = any(a <= t <= b for a, b in spans) or (
                bool(window) and prompted_share(window, other_w) >= prompt_max
            )
            steps.append(None if muted else (score, zu, ze, zs, u, reaction_share(window)))
        else:
            steps.append(None)
        rU.push(u)
        rE.push(e)
        rS.push(s)

    # Small channels: never ask for more unique chatters than the busiest 0.5% of windows reach.
    us = sorted(st[4] for st in steps if st)
    if us:
        q = us[int(0.995 * (len(us) - 1))]
        u_need = max(int(cfg.get("min_unique_floor", 3)), min(u_need, q))

    series = x_normal_series(msgs, bucket_s)
    found: list[Candidate] = []
    t, last_fire = 0, -1e9
    while t < len(steps):
        st = steps[t]
        if st is None or st[0] < fire_z or st[4] < u_need or st[5] < min_react or t - last_fire < cooldown:
            t += 1
            continue
        # best score within the cooldown span after the first fire
        end = min(len(steps), t + int(cooldown))
        best_t = max(
            (i for i in range(t, end) if steps[i] is not None and steps[i][4] >= u_need),  # type: ignore[index]
            key=lambda i: steps[i][0],  # type: ignore[index]
        )
        first = t
        while first - 1 >= 0 and steps[first - 1] is not None and steps[first - 1][0] >= pre_z:  # type: ignore[index]
            first -= 1
        peak = max(0.0, first - delay_s)
        bs = steps[best_t]
        assert bs is not None
        found.append(
            Candidate(
                stream=streamer,
                peak_s=peak,
                start_s=max(0.0, peak - before_s),
                end_s=peak + after_s,
                chat_score=bs[0],
                final_score=bs[0],
                x_normal=x_normal_at(series, float(first), bucket_s),
                unique_chatters=bs[4],
                signals={"zU": bs[1], "zE": bs[2], "zS": bs[3], "fire_s": float(t)},
            )
        )
        last_fire = t
        t += 1
    found.sort(key=lambda c: -c.chat_score)
    kept: list[Candidate] = []
    for c in found:
        if all(abs(c.peak_s - k.peak_s) >= gap_s for k in kept):
            kept.append(c)
        if len(kept) >= top:
            break
    return kept


def confirm(
    candidate: Candidate,
    viewer_clip_times: Sequence[float] = (),
    loud_z: float | None = None,
    clip_tol_s: float = 30.0,
) -> Candidate:
    """Optional confirmation hooks: viewer-made clips near the peak and audio loudness z-score."""
    bonus = 0.0
    sig = dict(candidate.signals)
    if any(abs(t - candidate.peak_s) <= clip_tol_s for t in viewer_clip_times):
        bonus += 1.5
        sig["viewer_clip"] = 1.0
    if loud_z is not None and loud_z > 0:
        bonus += min(loud_z, 5.0) * 0.3
        sig["loud_z"] = loud_z
    return replace(candidate, final_score=candidate.chat_score + bonus, signals=sig)
