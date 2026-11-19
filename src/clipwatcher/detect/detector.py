"""D3/D13 spike detector: chat_score = weighted z of unique chatters (U), messages (M), keyword energy (E) and
short/repeated share (S), each measured against the whole stream so far (detect.session.SessionTable)."""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import groupby
from typing import Any

from clipwatcher import config
from clipwatcher.detect.baseline import x_normal_at, x_normal_series
from clipwatcher.detect.session import SessionTable
from clipwatcher.models import Candidate, ChatMessage

_FLOORS = {"U": 1.5, "M": 2.0, "E": 2.0, "S": 0.08}
_WEIGHTS = {"U": 0.3, "M": 0.25, "E": 0.3, "S": 0.15}
_SUB_BOMB_TYPES = {"submysterygift", "subgift"}
_SUB_BOMB_MIN = 5
_EPOCH_S = 10**8  # timestamps above this are wall-clock (a saved live log), below it seconds into a VOD


@dataclass
class _Rec:
    user: str
    weight: float
    short: bool
    norm: str


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


def _held_keys(keyword: str) -> str:
    """Regex for a keyword that also matches it typed with held keys: LMAOOO, KEKWW, WWWW, LETS GOOO.

    The last letter of a word may repeat any number of times (lmaoo, nahh) and so may a keyword that is one
    letter throughout (W, kkkk, ?????: its own count is the minimum). A letter inside a word counts as held
    only from three on (loool): twice is usually another word (god / good, bot / boot).
    """
    words = []
    for word in keyword.split():
        runs = [(ch, len(list(run))) for ch, run in groupby(word)]
        parts = []
        for i, (ch, n) in enumerate(runs):
            c = re.escape(ch)
            if len(runs) == 1 or i == len(runs) - 1:
                parts.append(f"{c}{{{n},}}" if n > 1 else f"{c}+")
            else:
                parts.append(f"{c}{{{n}}}(?:{c}{{2,}})?")
        words.append("".join(parts))
    return r"\s+".join(words)


def _kw_patterns(keywords: dict[str, float]) -> list[tuple[str, re.Pattern[str], float]]:
    return [
        (k, re.compile(rf"(?<!\w){_held_keys(k)}(?!\w)", re.IGNORECASE), float(w))
        for k, w in keywords.items()
    ]


def settings(streamer: str) -> dict[str, Any]:
    """Detector settings for one streamer: config/global.yaml ``detector`` with per-streamer overrides."""
    cfg = config.global_config().get("detector", {})
    sc = config.streamer(streamer)
    return {
        "weights": dict(cfg.get("weights") or _WEIGHTS),  # a column without a weight does not count
        "win": int(cfg.get("window_s", 10)),
        "level_s": int(float(cfg.get("level_min", 60)) * 60),
        "session_share": float(cfg.get("level_session_share", 0.5)),
        "max_rows": int(float(cfg.get("session_max_h", 24)) * 3600),
        "fire_z": float(cfg.get("fire_z", 3.0)),
        "pre_z": float(cfg.get("pre_z", 2.0)),
        "keep_z": float(cfg.get("keep_z", 5.0)),
        "fire_q": float(sc.get("fire_q", cfg.get("fire_q", 0.98))),
        "keep_q": float(sc.get("keep_q", cfg.get("keep_q", 0.99))),
        "floor_z": float(cfg.get("floor_z", 2.0)),
        "session_min_s": int(cfg.get("session_min_s", 900)),
        "max_per_hour": int(sc.get("max_per_hour", cfg.get("max_per_hour", 8))),
        "rescue_s": float(cfg.get("rescue_s", 600)),
        "onset_max_s": float(cfg.get("onset_max_s", 15)),
        "min_unique": int(sc.get("min_unique", cfg.get("min_unique", 8))),
        "min_unique_floor": int(cfg.get("min_unique_floor", 3)),
        "pct": float(cfg.get("min_unique_viewer_pct", 0.01)),
        "mute_s": float(cfg.get("mute_after_notice_s", 75)),
        "cooldown": float(cfg.get("cooldown_s", 45)),
        "mature_s": float(cfg.get("live_mature_s", 15)),
        "delay_s": float(sc.get("delay_s", cfg.get("default_delay_s", 8))),
        "before_s": float(cfg.get("before_s", 20)),
        "after_s": float(cfg.get("after_s", 10)),
        "other_weight": float(cfg.get("other_weight", 0.5)),
        "prompt_share": float(cfg.get("prompt_share", 0.4)),
        "laughter": cfg.get("laughter_regex", ""),
    }


def new_table(p: Mapping[str, Any]) -> SessionTable:
    return SessionTable(
        _FLOORS, p["weights"], level_s=p["level_s"], session_share=p["session_share"], max_rows=p["max_rows"]
    )


def gate(table: SessionTable, p: Mapping[str, Any]) -> tuple[float, float]:
    """(fire, keep) score thresholds.

    Once the stream has ``session_min_s`` of history a spike is measured against the stream itself: it fires
    in the top ``1 - fire_q`` of all seconds so far and is kept when its peak reaches the top ``1 - keep_q``.
    A fixed z threshold cannot do this: 5.0 is routine in a 3-chatter chat and was never reached in three
    hours of a 100k-viewer one. Before that, or with fire_q 0, the fixed fire_z / keep_z apply.
    """
    if p["fire_q"] and table.scored >= p["session_min_s"]:
        fire = max(p["floor_z"], table.quantile(p["fire_q"]) or 0.0)
        return fire, max(fire, table.quantile(p["keep_q"] or p["fire_q"]) or 0.0)
    return p["fire_z"], p["keep_z"]


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
        toks = m.text.split()
        out.append((m.ts_ms // 1000, _Rec(m.user, wcache[m.text], len(toks) <= 3, _norm(m.text))))
    return out


def _mute_spans(msgs: Sequence[ChatMessage], mute_s: float, min_unique: int) -> list[tuple[float, float]]:
    """Raid / sub-bomb notices and giveaway bursts (bot message then one word typed by most chatters)."""
    spans: list[tuple[float, float]] = []
    notices = [m for m in msgs if m.is_notice]
    for m in notices:
        t = m.ts_ms / 1000
        recent = [n for n in notices if 0 <= (n.ts_ms / 1000 - t) <= 10]
        bomb = m.notice_type in _SUB_BOMB_TYPES and len(recent) >= _SUB_BOMB_MIN and recent[0] is m
        if m.notice_type == "raid" or mystery_bomb(m) or bomb:
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


def mystery_bomb(m: ChatMessage) -> bool:
    """A mystery gift of 5+ subs (or of unknown size): chat thanks the gifter, that is not a moment."""
    return m.notice_type == "submysterygift" and (m.viewers is None or m.viewers >= _SUB_BOMB_MIN)


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def window_features(window: Sequence[_Rec]) -> tuple[int, int, float, float]:
    """(U unique chatters, M messages, E keyword energy, S short/repeated share) of one 10 s window."""
    if not window:
        return 0, 0, 0.0, 0.0
    best: dict[str, float] = {}
    for r in window:
        if r.weight > best.get(r.user, -1):
            best[r.user] = r.weight
    counts = Counter(r.norm for r in window)
    s = sum(1 for r in window if r.short or counts[r.norm] > 1) / len(window)
    return len(best), len(window), sum(best.values()), s


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
    """Top moments of a finished chat. All of it is known, so every second is scored against the whole."""
    p = settings(streamer)
    win, cooldown, other_w = p["win"], p["cooldown"], p["other_weight"]
    if delay_s is None:
        delay_s = p["delay_s"]
    u_need = max(p["min_unique"], p["pct"] * viewers if viewers else 0)

    msgs = sorted(messages, key=lambda m: m.ts_ms)
    if not msgs:
        return []
    # A saved live log is stamped with wall-clock time: count from its first second, not from 1970.
    first_s = int(msgs[0].ts_ms // 1000)
    base = first_s if first_s >= _EPOCH_S else 0
    if base:
        msgs = [replace(m, ts_ms=m.ts_ms - base * 1000) for m in msgs]
    recs = _build_records(msgs, config.keywords_for(streamer), other_w, p["laughter"])
    spans = _mute_spans(msgs, p["mute_s"], p["min_unique"])
    horizon = int(msgs[-1].ts_ms // 1000) + 1
    per_sec: list[list[_Rec]] = [[] for _ in range(horizon + 1)]
    for sec, r in recs:
        per_sec[sec].append(r)

    table = new_table(p)
    table.auto = False
    feats: list[tuple[int, int, float, float]] = []
    muted: list[bool] = []
    for t in range(horizon + 1):
        window = [r for s in range(max(0, t - win + 1), t + 1) for r in per_sec[s]]
        f = window_features(window)
        mute = any(a <= t <= b for a, b in spans) or (
            bool(window) and prompted_share(window, other_w) >= p["prompt_share"]
        )
        feats.append(f)
        muted.append(mute)
        table.push(*f, scorable=t >= 60 and not mute, sec=t)
    table.refresh()
    fire_z, _ = gate(table, p)
    pre_z = fire_z * p["pre_z"] / p["fire_z"]
    lo = len(feats) - len(table)  # rows a marathon dropped from the table are not scored
    steps: list[tuple[float, dict[str, float], int] | None] = [
        None if muted[t] or t < 60 or t < lo else (*table.score(*table.row(t - lo)), f[0])
        for t, f in enumerate(feats)
    ]

    # Small channels: never ask for more unique chatters than the busiest 0.5% of windows reach.
    us = sorted(st[2] for st in steps if st)
    if us:
        q = us[int(0.995 * (len(us) - 1))]
        u_need = max(p["min_unique_floor"], min(u_need, q))

    series = x_normal_series(msgs, bucket_s)
    found: list[Candidate] = []
    t, last_fire = 0, -1e9
    while t < len(steps):
        st = steps[t]
        if st is None or st[0] < fire_z or st[2] < u_need or t - last_fire < cooldown:
            t += 1
            continue
        # best score within the cooldown span after the first fire
        end = min(len(steps), t + int(cooldown))
        best_t = max(
            (i for i in range(t, end) if steps[i] is not None and steps[i][2] >= u_need),  # type: ignore[index]
            key=lambda i: steps[i][0],  # type: ignore[index]
        )
        # The moment is where the burst that reached that score began: walk back from the best second, but
        # no further than a burst takes to build, or a long lively stretch drags the clip to its start.
        first = best_t
        while (
            first - 1 >= 0
            and best_t - (first - 1) <= p["onset_max_s"]
            and steps[first - 1] is not None
            and steps[first - 1][0] >= pre_z  # type: ignore[index]
        ):
            first -= 1
        peak = max(0.0, first - delay_s)
        bs = steps[best_t]
        assert bs is not None
        found.append(
            Candidate(
                stream=streamer,
                peak_s=base + peak,
                start_s=base + max(0.0, peak - before_s),
                end_s=base + peak + after_s,
                chat_score=bs[0],
                final_score=bs[0],
                x_normal=x_normal_at(series, float(first), bucket_s),
                unique_chatters=bs[2],
                signals={
                    **{f"z{k}": v for k, v in bs[1].items()},
                    "fire_s": float(base + t),
                    "gate": fire_z,
                    "session_pct": table.share_below(bs[0]),
                },
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
    for rank, c in enumerate(sorted(found, key=lambda c: -c.chat_score), 1):
        c.signals["session_rank"] = float(rank)
    return kept


def loud_z(
    series: Sequence[tuple[float, float]], peak_rel: float, before_s: float = 5.0, after_s: float = 12.0
) -> float | None:
    """How far the loudest instant around the moment stands above the clip's usual level, as a robust z.

    series = [(seconds from the window start, loudness in dB)], peak_rel = the moment in the same seconds.
    None without audio. A scream or a crash where chat spiked is D3's second confirmation of a moment.
    """
    levels = sorted(v for _, v in series if v > -70.0)  # below that is digital silence, not a level
    near = [v for t, v in series if peak_rel - before_s <= t <= peak_rel + after_s and v > -70.0]
    if len(levels) < 20 or not near:
        return None
    med = _median(levels)
    mad = _median(sorted(abs(v - med) for v in levels))
    return (max(near) - med) / max(1.4826 * mad, 1.0)


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
