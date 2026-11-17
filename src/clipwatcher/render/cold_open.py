"""Cold open: replay the strongest reaction, zoomed in, before the clip starts (stops the scroll).

The reaction instant comes from the judge (pk, it sees frames and transcript). Without the judge it is
a shout near the chat spike: much louder than the streamer's usual talking, with an exclamation said
right there. Nothing clear means no cold open, because a random talking fragment is worse than none.
The cold open is cut on word boundaries so it never starts or ends mid-word.
"""

from __future__ import annotations

import re
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipwatcher import config
from clipwatcher.models import Word

PRE_S = 0.3  # cold open starts this long before the reaction onset
MIN_INTO_CLIP_S = 3.0  # a reaction already in the first 3 s needs no cold open
MAX_WORD_TAIL_S = 0.8  # extend the cold open to finish the word in progress at most this far
EXCESS_DB = 8.0  # shout = this much louder than the streamer's usual talking level
SILENCE_DB = -40.0  # frames below this are pauses, not talking
EXCLAMATIONS = {
    "oh", "ooh", "no", "nooo", "noo", "nah", "wait", "bro", "bruh", "yo", "ayo", "wow", "omg", "dude",
    "holy", "damn", "shit", "fuck", "fucking", "hell", "lets", "let's", "haha", "hahaha", "lol", "huh",
    "stop", "run", "ahh", "ah", "aah", "jesus", "god",
}  # fmt: skip
SEARCH_BEFORE_S = 15.0  # look for the reaction this far before the chat spike...
SEARCH_AFTER_S = 3.0  # ...and this far after it


def settings() -> tuple[float, float]:
    """(duration s, zoom factor) from render.cold_open_s / render.cold_open_zoom; duration 0 disables."""
    r = config.global_config().get("render", {})
    return float(r.get("cold_open_s", 2.0)), float(r.get("cold_open_zoom", 1.15))


def loudness(media: Path, start: float, dur: float) -> list[tuple[float, float]]:
    """EBU R128 momentary loudness (400 ms) every 100 ms as (t relative to start, LUFS). [] on failure."""
    coarse = max(0.0, start - 6)  # D9: coarse input seek, exact trim in the graph
    cmd = [
        config.ffmpeg_bin(), "-nostats", "-hide_banner", "-ss", f"{coarse:.3f}", "-i", str(media), "-vn",
        "-af", f"atrim=start={start - coarse:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,"
        "ebur128=metadata=1,ametadata=print:key=lavfi.r128.M:file=-", "-f", "null", "-",
    ]  # fmt: skip
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    series, t = [], None
    for line in out.splitlines():
        if m := re.search(r"pts_time:([\d.]+)", line):
            t = float(m.group(1))
        elif (m := re.search(r"lavfi\.r128\.M=(-?[\d.]+|-inf)", line)) and t is not None:
            v = float(m.group(1)) if m.group(1) != "-inf" else -70.0
            if not series or t - series[-1][0] >= 0.099:
                series.append((t, max(v, -70.0)))
    return series


def _excited(w: Word) -> bool:
    t = w.text.strip().lower()
    return t.endswith("!") or t.strip(".,!?…'\"") in EXCLAMATIONS


def audio_reaction(
    series: list[tuple[float, float]], lo: float, hi: float, words: list[Word] | None = None
) -> float | None:
    """Onset of a shout inside [lo, hi], or None when nothing clearly stands out.

    A shout is momentary loudness (1 s average) EXCESS_DB above the streamer's usual talking level (median
    of the non-silent frames) with an exclamation spoken right there. Loudness alone is not enough: the
    streamer starting to talk after a pause is loud too. Without words the bar is EXCESS_DB + 4.
    """
    speech = [v for _, v in series if v > SILENCE_DB]
    if len(speech) < 20:
        return None
    base = statistics.median(speech)
    smooth = []
    for i, (t, _) in enumerate(series):
        win = [v for _, v in series[max(0, i - 5) : i + 6]]
        smooth.append((t, sum(win) / len(win)))
    best, best_v = None, None
    for i, (t, v) in enumerate(smooth):
        if not lo <= t <= hi:
            continue
        if words:
            if v - base < EXCESS_DB or not any(t - 1.0 <= w.start <= t + 1.5 and _excited(w) for w in words):
                continue
        elif v - base < EXCESS_DB + 4:
            continue
        if best_v is None or v > best_v:
            best, best_v = i, v
    if best is None:
        return None
    i = best  # onset: walk back while still clearly above the talking level, at most 1 s
    while i > 0 and smooth[i - 1][1] >= base + 3 and smooth[best][0] - smooth[i - 1][0] <= 1.0:
        i -= 1
    return round(smooth[i][0], 2)


def window(
    reaction_rel: float | None, trim_start: float, dur: float, words: list[Word] | None = None
) -> tuple[float, float] | None:
    """(start, duration) of the cold open relative to the trimmed clip start, or None.

    reaction_rel and words are relative to the candidate window; trim_start/dur describe the trimmed clip.
    """
    co_s, _ = settings()
    if reaction_rel is None or co_s <= 0 or dur < co_s + MIN_INTO_CLIP_S:
        return None
    p = reaction_rel - trim_start
    if p < MIN_INTO_CLIP_S or p > dur:
        return None
    s0 = reaction_rel - PRE_S
    e0 = s0 + co_s
    s, e = s0, e0
    for w in words or []:
        if w.start < s0 < w.end:  # never start mid-word
            s = w.start - 0.08
        if w.start < e0 < w.end:  # finish the word in progress, or stop before it
            e = w.end + 0.08 if w.end - e0 <= MAX_WORD_TAIL_S else w.start - 0.02
    s = max(s, trim_start)
    e = min(e, trim_start + dur)
    if e - s < 1.0:
        return None
    return round(s - trim_start, 2), round(e - s, 2)


@dataclass
class Plan:
    t0: float  # trim, relative to the candidate window
    t1: float
    reaction: float | None  # relative to the candidate window
    cold_open: tuple[float, float] | None  # relative to the trimmed clip start


def plan(
    media: Path, win_start: float, win_dur: float, chat_peak_rel: float, jr: Any, words: list[Word]
) -> Plan:
    """Trim + cold open for one candidate. jr is a JudgeResult or its dict; words relative to the window."""
    from clipwatcher.judge.snap import heuristic_trim, reaction_s, snap_trim

    get = jr.get if isinstance(jr, dict) else lambda k, d=None: getattr(jr, k, d)
    judged = get("source") == "claude"
    react = get("peak_rel_s") if judged else None
    if react is None and settings()[0] > 0:
        series = loudness(media, win_start, win_dur)
        react = audio_reaction(series, chat_peak_rel - SEARCH_BEFORE_S, chat_peak_rel + SEARCH_AFTER_S, words)
    t0, t1 = float(get("trim_start_s") or 0.0), float(get("trim_end_s") or win_dur)
    if not judged:
        t0, t1 = heuristic_trim(react if react is not None else reaction_s(None, chat_peak_rel), win_dur)
        t0, t1 = snap_trim(t0, t1, words, win_dur)
    if not (0 <= t0 < t1 <= win_dur + 0.5 and t1 - t0 >= 12):
        t0, t1 = 0.0, win_dur
    return Plan(t0, t1, react, window(react, t0, t1 - t0, words))
