"""Cold open: replay the strongest reaction, zoomed in, before the clip starts (stops the scroll).

It starts render.cold_open_lead_s (2.5 s) before the peak so the viewer hears what leads to it.

The reaction instant comes from the judge (pk): only it sees the frames, so only it knows when something
happens on screen. The audio then moves pk onto the loudest instant within +/-REFINE_S. Without the judge
there is no cold open (owner, 17 Nov: chat timing and loudness alone picked moments where nothing happens);
render.cold_open_audio_fallback: true re-enables the shout detector (audio_reaction) for experiments.
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

LEAD_S = 2.5  # default render.cold_open_lead_s: start this long before the peak (owner, 17 Nov: the build-up,
# not the middle of the shout, which alone is unintelligible)
MIN_INTO_CLIP_S = 3.0  # a reaction already in the first 3 s needs no cold open
SENTENCE_BACK_S = 3.0  # move the start back to the start of the sentence in progress at most this far
SENTENCE_GAP_S = 0.7  # a pause this long between words is a sentence boundary too
MAX_WORD_TAIL_S = 0.8  # extend the cold open to finish the word in progress at most this far
END_PAD_S = 0.25  # silence kept after the last word of the cold open (the cut swallowed the last syllable)
EXCESS_DB = 8.0  # shout = this much louder than the streamer's usual talking level
SILENCE_DB = -40.0  # frames below this are pauses, not talking
EXCLAMATIONS = {
    "oh", "ooh", "no", "nooo", "noo", "nah", "wait", "bro", "bruh", "yo", "ayo", "wow", "omg", "dude",
    "holy", "damn", "shit", "fuck", "fucking", "hell", "lets", "let's", "haha", "hahaha", "lol", "huh",
    "stop", "run", "ahh", "ah", "aah", "jesus", "god",
}  # fmt: skip
SEARCH_BEFORE_S = 15.0  # look for the reaction this far before the chat spike...
SEARCH_AFTER_S = 3.0  # ...and this far after it
REFINE_S = 1.5  # move the judge's pk to the loudest instant this close to it


def settings() -> tuple[float, float]:
    """(duration s, zoom factor) from render.cold_open_s / render.cold_open_zoom; duration 0 disables."""
    r = config.global_config().get("render", {})
    return float(r.get("cold_open_s", 2.0)), float(r.get("cold_open_zoom", 1.15))


def lead() -> float:
    """Seconds of build-up before the peak (render.cold_open_lead_s)."""
    return float(config.global_config().get("render", {}).get("cold_open_lead_s", LEAD_S))


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
    s0 = reaction_rel - lead()
    e0 = s0 + co_s
    s, e = sentence_start(s0, words or []), e0
    for w in words or []:
        if w.start < e0 < w.end:  # finish the word in progress, or stop before it
            e = w.end + END_PAD_S if w.end - e0 <= MAX_WORD_TAIL_S else w.start - 0.02
        elif e0 - END_PAD_S < w.end <= e0:  # a word just ended: let its last syllable ring out
            e = max(e, w.end + END_PAD_S)
    s = max(s, trim_start)
    e = min(e, trim_start + dur)
    if e - s < 1.0:
        return None
    return round(s - trim_start, 2), round(e - s, 2)


def sentence_start(t: float, words: list[Word]) -> float:
    """Start of the sentence being spoken at t (owner, 17 Nov: the joke starts with the sentence, not
    mid-way), at most SENTENCE_BACK_S earlier; a word start just before t otherwise; t in a pause."""
    from clipwatcher.judge.snap import _ends_sentence

    ws = sorted((w for w in words if w.text.strip()), key=lambda w: w.start)
    i = next((k for k, w in enumerate(ws) if w.end > t), None)
    if i is None or ws[i].start > t + 0.05:  # t falls in a pause: the next sentence starts after it
        if i is None or i == 0 or ws[i].start - ws[i - 1].end >= SENTENCE_GAP_S or _ends_sentence(ws[i - 1]):
            return t
    k, found = i, False
    while k > 0:
        prev = ws[k - 1]
        if _ends_sentence(prev) or ws[k].start - prev.end >= SENTENCE_GAP_S:
            found = True
            break
        if t - prev.start > SENTENCE_BACK_S:
            break
        k -= 1
    found = found or k == 0
    if not found or t - ws[k].start > SENTENCE_BACK_S:  # no sentence start close by: only the word
        return round(ws[i].start - 0.08, 2) if ws[i].start < t else t
    return min(t, round(ws[k].start - 0.08, 2))


def refine(series: list[tuple[float, float]], pk: float) -> float:
    """Judge pk moved onto the loudest (1 s average) instant within +/-REFINE_S, minus a short lead-in."""
    near = [(t, v) for t, v in series if abs(t - pk) <= REFINE_S]
    if len(near) < 5:
        return pk
    smooth = []
    for i, (t, _) in enumerate(near):
        win = [v for _, v in near[max(0, i - 3) : i + 4]]
        smooth.append((t, sum(win) / len(win)))
    t, _ = max(smooth, key=lambda x: x[1])
    return round(t, 2)


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
    fallback = bool(config.global_config().get("render", {}).get("cold_open_audio_fallback", False))
    if settings()[0] > 0 and (react is not None or fallback):
        series = loudness(media, win_start, win_dur)
        if react is not None:
            react = refine(series, float(react))
        else:
            lo, hi = chat_peak_rel - SEARCH_BEFORE_S, chat_peak_rel + SEARCH_AFTER_S
            react = audio_reaction(series, lo, hi, words)
    t0, t1 = float(get("trim_start_s") or 0.0), float(get("trim_end_s") or win_dur)
    if not judged:
        t0, t1 = heuristic_trim(react if react is not None else reaction_s(None, chat_peak_rel), win_dur)
        t0, t1 = snap_trim(t0, t1, words, win_dur)
    if not (0 <= t0 < t1 <= win_dur + 0.5 and t1 - t0 >= 12):
        t0, t1 = 0.0, win_dur
    return Plan(t0, t1, react, window(react, t0, t1 - t0, words))
