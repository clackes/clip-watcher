# ruff: noqa: E501
"""Frame-by-frame checks of the Remotion caption overlays.

Every frame of a render is checked against the plan it was rendered from (render/rm_plan.py), against the
scene the renderer reports for that frame (positions, scales, colours of every word: the DOM as drawn) and
against its own pixels. A case is one template, one fixture, one frame rate; `run_case` renders it with
every frame kept and returns a CaseResult listing each check, how often it ran and every failure.

Checks, per frame unless noted:
  visible_set      only the pages/labels the plan puts on this frame are drawn (rest states before and after)
  words_shown      every word that should be up is up (page mode: all words; build mode: the spoken ones)
  active_timing    (per word) the highlight goes on and off within one frame of the word's times
  highlight        (per page) the spoken word looks different from the same word when it is not spoken
  blank            a frame with nothing planned is fully transparent
  ink              every drawn word has pixels where the renderer says it is, and there is no ink elsewhere
  specks           no speck of video inside the caption: a see-through gap that ink walls in and that is
                   under 5 px across (the renderer closes them as the last step of a frame)
  safe_area        no ink outside the caption box (so never near the edges or in the platform dead zone)
  lines_clear      (per page) lines do not overlap; words_clear: neighbours on a line do not overlap
  jump / flash     position, scale, rotation and colour of every unit change without one-frame jumps or
                   one-frame excursions; lifetime: nothing is on screen for a single frame
  frame_change     pixels change when the scene changes and only then (no stutter, no shimmer)
  font             (per case) all glyphs come from the bundled fonts; drawn widths match the font file
  contrast         every fill against the pixels right next to it, composited over adversarial backgrounds
  fps_match        (per 30/60 pair) both frame rates show the same motion
  phase sweep      (run_sweep, --phases) all of the above again with the words moved by fractions of a frame:
                   a fixture tests the instants its word times happen to fall on, and a change that is spread
                   over three frames at one of them can land in a single frame at another
  deterministic    (per case, optional) a second render gives byte-identical frames
  composite        (per case, optional) ffmpeg's composite equals the exact composite on every frame

Needs numpy and Pillow; Node only for rendering. CLI: python -m clipwatcher.render.framecheck --help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path

from clipwatcher import config
from clipwatcher.models import Word
from clipwatcher.render import palette, rm_plan
from clipwatcher.render import remotion as rm

PAD = 110  # transparent rows above and below the caption box in check renders: overflow lands there
MIN_SCALE = 0.5  # below this (on its thinner axis) a word is mid-entrance or mid-exit: ink and contrast are not judged
EPS_JUMP = {"x": 14.0, "y": 14.0, "sc": 0.10, "rot": 4.0, "r": 40.0, "g": 40.0, "b": 40.0}
EPS_FLASH = {"x": 8.0, "y": 8.0, "sc": 0.08, "rot": 2.5, "r": 48.0, "g": 48.0, "b": 48.0}
MAX_FAILS = 40  # examples kept per check


# ------------------------------------------------------------------ fixtures


@dataclass(frozen=True)
class Fixture:
    name: str
    dur: float
    words: tuple[tuple[float, float, str], ...]
    beats: tuple[tuple, ...] = ()
    actions: tuple[tuple, ...] = ()
    box: str = "seam"  # seam | frame | reframe | stack (remotion.caption_box)

    def word_list(self) -> list[Word]:
        return [Word(s, e, t) for s, e, t in self.words]

    def shifted(self, phase: float, fps: float = 30.0) -> Fixture:
        """The same talk `phase` of a frame later (a frame at `fps`): every word, beat and action label
        keeps its length and its distance to the others, and all of them fall on other instants of the
        frame. The clip is one frame longer, so nothing is cut off that was not before."""
        if not phase:
            return self
        d = phase / fps
        return replace(
            self,
            name=f"{self.name}+{phase:g}",
            dur=round(self.dur + 1.0 / fps, 4),
            words=tuple((round(s + d, 4), round(e + d, 4), t) for s, e, t in self.words),
            beats=tuple((round(b[0] + d, 4), *b[1:]) for b in self.beats),
            actions=tuple((round(a[0] + d, 4), round(a[1] + d, 4), *a[2:]) for a in self.actions),
        )


def _seq(t0: float, step: float, text: str, gap: float = 0.0) -> tuple[tuple[float, float, str], ...]:
    """Words one after another: each lasts step - gap seconds."""
    return tuple(
        (round(t0 + i * step, 3), round(t0 + (i + 1) * step - gap, 3), w) for i, w in enumerate(text.split())
    )


FIXTURES: dict[str, Fixture] = {
    f.name: f
    for f in (
        # entrance, emphasis, numbers, punctuation, apostrophes, caps, a 20-letter word, an emoji, words with
        # no gap, a pause with an exit, a beat label and an action label
        Fixture(
            "mix",
            7.4,
            (
                *_seq(0.2, 0.3, "NO WAY! did you", 0.0),
                *_seq(1.4, 0.25, "see THAT, chat?", 0.02),
                *_seq(2.8, 0.3, "He's got 100% of it...", 0.05),
                (4.3, 4.9, "uncharacteristically"),
                *_seq(5.0, 0.28, "let's GOOO \U0001f602", 0.0),
            ),
            beats=((6.3, "hype", "*HYPE*"),),
            actions=((3.75, 4.25, "*STARES*"),),
        ),  # fmt: skip
        # the same in three seconds, for quick runs
        Fixture(
            "short",
            3.2,
            (*_seq(0.1, 0.3, "NO WAY! did you", 0.0), *_seq(1.6, 0.26, "see THAT 100%", 0.02)),
            beats=((2.5, "fail", "*FAIL*"),),
        ),  # fmt: skip
        # seven words a second without gaps, in the low box under a no-webcam frame
        Fixture(
            "fast",
            3.0,
            (
                *_seq(0.1, 1 / 7, "one two three four five six seven eight nine ten eleven twelve"),
                (2.2, 2.6, "done."),
            ),
            box="frame",
        ),  # fmt: skip
        # words that overlap in time, two with the same start, one that ends after the next began
        Fixture(
            "overlap",
            2.6,
            (
                (0.2, 0.9, "wait"),
                (0.5, 1.0, "what"),
                (0.8, 1.6, "is"),
                (0.8, 1.2, "that"),
                (1.3, 1.9, "bro"),
                (1.85, 2.3, "no"),
            ),
        ),  # fmt: skip
        Fixture("single", 1.4, ((0.0, 0.6, "Bruh"),)),  # one word, on the very first frame
        Fixture("empty", 0.6, ()),  # no transcript: every frame blank
        # long phrase, a 20-letter and a 30-letter word, in the lowest box (old stack layout)
        Fixture(
            "long",
            4.2,
            (
                *_seq(0.1, 0.22, "this is a really long sentence that just keeps on going"),
                (2.6, 3.1, "incomprehensibilities"),
                (3.2, 3.9, "Pneumonoultramicroscopicsilico"),
            ),
            box="stack",
        ),  # fmt: skip
        # two full lines in the tall box of a full-screen reframe
        Fixture(
            "tall",
            3.4,
            (*_seq(0.15, 0.27, "I can't BELIEVE that actually just happened"), (2.4, 2.9, "WOW!")),
            box="reframe",
        ),  # fmt: skip
        # found on a real clip: three shouted words in a row, each spoken while its letters still land, and
        # a last page that leaves letter by letter
        Fixture(
            "shouts",
            5.555,
            (
                (0.4, 0.612, "we"),
                (0.632, 0.761, "are"),
                (0.811, 1.043, "so"),
                (1.043, 1.215, "back"),
                (2.115, 2.245, "wait"),
                (2.295, 2.464, "wait"),
                (2.464, 2.676, "wait"),
                (2.726, 2.997, "why"),
                (2.997, 3.264, "would"),
                (3.314, 3.492, "you"),
                (3.542, 3.727, "do"),
                (3.747, 3.996, "that"),
                (4.016, 4.308, "what"),
                (4.328, 4.555, "is"),
            ),
        ),  # fmt: skip
    )
}
QUICK = ("short", "single", "empty")
FULL = ("mix", "fast", "overlap", "single", "empty", "long", "tall", "shouts")
# The phase sweep: the words moved by these fractions of a frame at SWEEP_FPS (a quarter of a frame at
# 30 fps is half a frame at 60), on the fixtures with the fastest talk.
PHASES = (0.0, 0.25, 0.5, 0.75)
SWEEP = ("shouts",)
SWEEP_FPS = 30.0


def pseudo_transcript(dur: float, seed: int = 7) -> list[Word]:
    """Made-up word timings for a clip without a transcript: phrases of streamer talk with pauses."""
    rnd = random.Random(seed)
    phrases = [
        "no way did you see that", "chat look at this", "he just went ALL IN", "what is he doing bro",
        "I can't believe it worked", "wait wait wait", "that's actually insane", "100% the best play",
        "let's GOOO", "oh my god", "run it back", "why would you do that", "we are so back",
        "this is not happening", "bro is cooked", "nah that's crazy",
    ]  # fmt: skip
    out: list[Word] = []
    t = 0.4
    while t < dur - 1.0:
        for w in rnd.choice(phrases).split():
            d = rnd.uniform(0.16, 0.34)
            if t + d > dur - 0.3:
                break
            out.append(Word(round(t, 3), round(t + d - rnd.choice([0.0, 0.02, 0.05]), 3), w))
            t += d
        t += rnd.choice([0.0, 0.15, 0.5, 0.9, 1.4])
    return out


# ------------------------------------------------------------------ results


@dataclass
class CaseResult:
    template: str
    fixture: str
    fps: float
    colors: str
    frames: int = 0
    checks: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # check -> times it ran
    fails: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    fail_count: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    stats: dict[str, float] = field(default_factory=dict)
    render_s: float = 0.0

    def ran(self, check: str, n: int = 1) -> None:
        self.checks[check] += n

    def fail(self, check: str, msg: str) -> None:
        self.fail_count[check] += 1
        if len(self.fails[check]) < MAX_FAILS:
            self.fails[check].append(msg)

    @property
    def ok(self) -> bool:
        return not self.fail_count

    @property
    def total_checks(self) -> int:
        return sum(self.checks.values())

    @property
    def total_fails(self) -> int:
        return sum(self.fail_count.values())

    def summary(self) -> str:
        head = f"{self.template}/{self.fixture}@{self.fps:g} {self.colors}: {self.frames} frames, {self.total_checks} checks"
        if self.ok:
            return head + ", all passed"
        lines = [head + f", {self.total_fails} FAILED"]
        for k, n in sorted(self.fail_count.items()):
            lines.append(f"  {k}: {n} of {self.checks.get(k, 0)}")
            lines += [f"    {m}" for m in self.fails[k][:6]]
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "template": self.template, "fixture": self.fixture, "fps": self.fps, "colors": self.colors,
            "frames": self.frames, "checks": dict(self.checks), "fail_count": dict(self.fail_count),
            "fails": {k: v for k, v in self.fails.items()}, "stats": self.stats, "render_s": self.render_s,
        }  # fmt: skip


# ------------------------------------------------------------------ loading a render


def _np():
    import numpy as np

    return np


def load_debug(path: Path) -> tuple[dict, dict[int, dict]]:
    """(the INIT record, frame number -> scene record) from the renderer's debug log."""
    init, frames = {}, {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("INIT"):
            init = init or json.loads(line[4:])
        elif line.startswith("{"):
            rec = json.loads(line)
            frames[rec["f"]] = rec
    return init, frames


def load_frame(d: Path, f: int):
    from PIL import Image

    np = _np()
    with Image.open(d / f"{f:06d}.png") as im:
        return np.asarray(im.convert("RGBA"))


def _lum(np, rgb):
    """WCAG relative luminance of an (..., 3) float array of 0..255 sRGB values."""
    c = rgb / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]


# ------------------------------------------------------------------ synthetic backgrounds


def background(kind: str, f: int, h: int, w: int, y0: int = 0, x0: int = 0):
    """An adversarial background (h, w, 3) uint8: the window at (y0, x0) of frame f. kind: white | black |
    #RRGGBB | flash | noise | stripes. Deterministic in (kind, f, y0, x0)."""
    np = _np()
    if kind == "white":
        return np.full((h, w, 3), 255, np.uint8)
    if kind == "black":
        return np.zeros((h, w, 3), np.uint8)
    if kind.startswith("#"):
        return np.broadcast_to(np.array(palette.parse_hex(kind), np.uint8), (h, w, 3)).copy()
    if kind == "flash":  # white and black alternating every two frames
        return np.full((h, w, 3), 255 if (f // 2) % 2 == 0 else 0, np.uint8)
    if kind == "noise":  # every pixel a random colour, new on every frame
        return np.random.default_rng((1000 + f, y0, x0)).integers(0, 256, (h, w, 3), dtype=np.uint8)
    if kind in ("stripes", "blocks"):  # saturated 24 px stripes scrolling sideways
        names = ("yellow", "black", "cyan", "white", "pink", "lime")
        cols = np.array([palette.parse_hex(palette.PALETTE[n]) for n in names], np.uint8)
        # "blocks" moves by whole chroma samples, so a 4:2:0 video can hold it exactly
        x = (np.arange(x0, x0 + w) + f * (8 if kind == "blocks" else 7)) // 24 % len(cols)
        return np.broadcast_to(cols[x][None, :, :], (h, w, 3)).copy()
    raise ValueError(f"unknown background {kind}")


SYNTH = ("white", "black", "flash", "noise", "stripes")


# ------------------------------------------------------------------ the checks


def thin(u: dict) -> float:
    """Scale of a unit along its thinner axis: 1 at rest, 0.3 when squashed to 30 % in one direction. The
    outline is that much thinner there, so this is what "big enough to be judged" and the width of the
    measuring ring go by."""
    m = u["m"]
    return min(math.hypot(m[0], m[1]), math.hypot(m[2], m[3]))


def visible_share(u: dict) -> float:
    """Share of a unit's box that its clip window lets through (1 without a window)."""
    if not u["clip"] or not u["box"]:
        return 1.0
    x, y, w, h = u["box"]
    cx, cy, cw, ch = u["clip"]
    iw = min(x + w, cx + cw) - max(x, cx)
    ih = min(y + h, cy + ch) - max(y, cy)
    return max(0.0, iw) * max(0.0, ih) / max(1e-6, w * h)


def _expected(plan: rm_plan.Plan, T: float) -> tuple[set[int], set[int]]:
    return (
        {p.id for p in plan.pages if p.start <= T < p.end},
        {lb.id for lb in plan.labels if lb.start <= T < lb.end},
    )


def _near_edge(spans: list[tuple[float, float]], T: float, tol: float) -> bool:
    return any(abs(T - a) <= tol or abs(T - b) <= tol for a, b in spans)


def _unit_boxes(rec: dict) -> dict[tuple[int, int, int], list[float]]:
    """(label flag, page, word) -> union box [x0, y0, x1, y1] of its units on this frame. Emoji are left
    out: their box is the font's em box, wider than the picture, not their ink."""
    out: dict[tuple[int, int, int], list[float]] = {}
    for u in rec["u"]:
        if not u["box"] or u.get("em"):
            continue
        x, y, w, h = u["box"]
        k = (u["lb"], u["p"], u["w"])
        b = out.get(k)
        out[k] = (
            [x, y, x + w, y + h]
            if b is None
            else [min(b[0], x), min(b[1], y), max(b[2], x + w), max(b[3], y + h)]
        )
    return out


def check_plan_frames(
    res: CaseResult, plan: rm_plan.Plan, params: dict, frames: dict[int, dict], n: int, fps: float
) -> None:
    """visible_set, words_shown, active_timing, highlight, lifetime: the scene against the plan."""
    one = 1.0 / fps
    mode = params["paging"]["mode"]
    enter = float(params["timing"]["enterS"]) + 1e-6
    stagger = float(params["timing"]["staggerS"])
    pages = {p.id: p for p in plan.pages}
    labels = {lb.id: lb for lb in plan.labels}
    active: dict[tuple[int, int], list[int]] = defaultdict(list)
    seen: dict[str, list[int]] = defaultdict(list)
    for f in range(n):
        rec = frames.get(f)
        T = (f + 0.5) / fps
        res.ran("visible_set")
        if rec is None:
            res.fail("visible_set", f"frame {f}: no scene record")
            continue
        want_p, want_l = _expected(plan, T)
        got_p = {u["p"] for u in rec["u"] if not u["lb"]}
        got_l = {u["p"] for u in rec["u"] if u["lb"]}
        # a page may be drawn a frame early or late at its own start and end, never anywhere else
        extra_p = [
            i
            for i in got_p - want_p
            if i not in pages or not _near_edge([(pages[i].start, pages[i].end)], T, one)
        ]
        extra_l = [
            i
            for i in got_l - want_l
            if i not in labels or not _near_edge([(labels[i].start, labels[i].end)], T, one)
        ]
        if extra_p or extra_l:
            res.fail(
                "visible_set",
                f"frame {f}: drawn pages {sorted(got_p)} labels {sorted(got_l)}, planned {sorted(want_p)} / {sorted(want_l)}",
            )
        for u in rec["u"]:
            seen[u["id"]].append(f)
            if u["a"] and not u["lb"]:
                active[(u["p"], u["w"])].append(f)
        for pid in want_p:
            p = pages[pid]
            t = T - p.start
            have = {u["w"] for u in rec["u"] if not u["lb"] and u["p"] == pid and visible_share(u) > 0.2}
            for i, w in enumerate(p.words):
                res.ran("words_shown")
                if mode == "build":
                    due = T >= w.start + one
                    early = i not in have or T >= w.start - one
                    if not early:
                        res.fail(
                            "words_shown",
                            f"frame {f}: page {pid} word {i} {w.text!r} drawn {w.start - T:.3f}s before it is spoken",
                        )
                else:
                    due = t >= enter + stagger * i + one
                if due and i not in have and T < p.end - max(p.exit, one) - one:
                    res.fail("words_shown", f"frame {f}: page {pid} word {i} {w.text!r} missing at t={t:.3f}")
        for lid in want_l:
            res.ran("words_shown")
            lb = next(x for x in plan.labels if x.id == lid)
            if lid not in got_l and lb.start + enter + one <= T < lb.end - max(lb.exit, one) - one:
                res.fail("words_shown", f"frame {f}: label {lid} {lb.text!r} missing")
    for p in plan.pages:
        for i, w in enumerate(p.words):
            res.ran("active_timing")
            fr = sorted(set(active.get((p.id, i), [])))  # a word may be drawn as several units
            first, last = round(w.start * fps), round(min(w.active_end, p.end) * fps) - 1
            if last < first:
                continue
            if not fr:
                res.fail(
                    "active_timing",
                    f"page {p.id} word {i} {w.text!r} never highlighted (planned frames {first}-{last})",
                )
            elif abs(fr[0] - first) > 1 or abs(fr[-1] - last) > 1:
                res.fail(
                    "active_timing",
                    f"page {p.id} word {i} {w.text!r}: highlighted frames {fr[0]}-{fr[-1]}, planned {first}-{last}",
                )
            elif fr != list(range(fr[0], fr[-1] + 1)):
                res.fail("active_timing", f"page {p.id} word {i} {w.text!r}: highlight has gaps")
    for uid, fr in seen.items():
        res.ran("lifetime")
        runs, a = [], fr[0]
        for x, y in zip(fr, fr[1:], strict=False):
            if y != x + 1:
                runs.append((a, x))
                a = y
        runs.append((a, fr[-1]))
        short = [r for r in runs if r[1] - r[0] + 1 < 2]
        if short and n > 2:
            res.fail("lifetime", f"unit {uid} on screen for a single frame: {short[:3]}")
        gaps = [
            (runs[i][1], runs[i + 1][0]) for i in range(len(runs) - 1) if runs[i + 1][0] - runs[i][1] <= 2
        ]
        if gaps:
            res.fail("lifetime", f"unit {uid} blinks off for a frame: {gaps[:3]}")


def _signature(rec: dict, pid: int, word: int) -> tuple:
    us = sorted(
        (u for u in rec["u"] if not u["lb"] and u["p"] == pid and u["w"] == word), key=lambda u: u["id"]
    )
    return tuple(
        (tuple(u["fg"]), round(u["sc"], 2), tuple(round(v) for v in (u["box"] or [0, 0, 0, 0]))) for u in us
    )


def check_highlight(res: CaseResult, plan: rm_plan.Plan, frames: dict[int, dict], fps: float) -> None:
    """The spoken word must look different from itself while another word of the page is spoken."""
    for p in plan.pages:
        if len(p.words) < 2:
            continue
        mids = [round((w.start + min(w.active_end, p.end)) / 2 * fps) for w in p.words]
        for i in range(len(p.words)):
            j = (i + 1) % len(p.words)
            a, b = frames.get(mids[i]), frames.get(mids[j])
            if not a or not b or p.words[i].active_end - p.words[i].start < 2.5 / fps:
                continue
            res.ran("highlight")
            sa, sb = _signature(a, p.id, i), _signature(b, p.id, i)
            shapes = [
                (s["id"], s["fill"], tuple(round(v) for v in (s["box"] or [0, 0, 0, 0]))) for s in a["s"]
            ]
            shapes_b = [
                (s["id"], s["fill"], tuple(round(v) for v in (s["box"] or [0, 0, 0, 0]))) for s in b["s"]
            ]
            if sa and sb and sa == sb and shapes == shapes_b:
                res.fail(
                    "highlight",
                    f"page {p.id} word {i} {p.words[i].text!r} looks the same spoken (frame {mids[i]}) and not (frame {mids[j]})",
                )


def check_layout(res: CaseResult, init: dict, params: dict, props: dict) -> None:
    """lines_clear from the layout the renderer reports, and the font report."""
    res.ran("font")
    if not init:
        res.fail("font", "the renderer reported no layout (INIT record missing)")
        return
    if init.get("missing"):
        res.fail("font", f"glyphs not in the bundled fonts: {init['missing'][:5]}")
    if init.get("fonts", [])[:1] != [params["font"]["file"]] or init.get("emoji") != props["emojiFamily"]:
        res.fail(
            "font",
            f"fonts loaded {init.get('fonts')} + {init.get('emoji')!r}, wanted {params['font']['file']}",
        )
    m = init.get("metrics", {})
    upper = params["case"] == "upper"
    for pg in init.get("pages", []):
        lines = pg["lines"]
        for a, b in zip(lines, lines[1:], strict=False):
            res.ran("lines_clear")
            bottom = a["y"] + (0.03 if upper else m.get("desc", 0.25)) * pg["size"]
            top = (
                b["y"]
                - (m.get("capH", 0.7) if upper else max(m.get("capH", 0.7), m.get("asc", 0.75))) * pg["size"]
            )
            if top - bottom < -0.04 * pg["size"]:
                res.fail(
                    "lines_clear",
                    f"page {pg['id']}: lines overlap by {bottom - top:.1f}px at size {pg['size']}",
                )


def _reached(np, clear):
    """The clear pixels that a path of clear pixels (up, down, left, right) joins to the outside of the
    picture. A whole run of clear pixels at a time, along the rows and then the columns, until nothing is
    added. (Pillow's flood fill leaves an image made from an array untouched, without a word: until 20 Nov
    this was one, and every clear pixel near a compact shape counted as walled in.)"""
    c = np.pad(clear, 1, constant_values=True)
    r = np.zeros_like(c)
    r[0, :] = r[-1, :] = r[:, 0] = r[:, -1] = True

    def sweep(c, r):  # a run with a reached pixel in it is reached whole
        ids = np.cumsum(~c, axis=1) + np.arange(c.shape[0])[:, None] * (c.shape[1] + 1)
        hit = np.zeros(c.shape[0] * (c.shape[1] + 1) + 1, bool)
        hit[ids[r]] = True
        return c & hit[ids]

    n = -1
    while n != (n := int(r.sum())):
        r = sweep(c, r)
        r = sweep(c.T, r.T).T
    return r[1:-1, 1:-1]


def speck_mask(np, alpha):
    """The transparent pixels in specks that ink walls in, as a mask the size of the picture: inside a
    counter an outline has all but closed, or between two outlines that touch above and below. A
    see-through region counts when no path of transparent pixels leads from it to the outside of the
    drawing and no part of it is 5 px across (the renderer fills counters whose opening would be under
    7 px, so anything smaller is a defect). The open counters of big letters and the gaps between words do
    not count."""
    clear = alpha < 128
    out = np.zeros(alpha.shape, bool)
    if not (~clear).any():
        return out
    ys, xs = np.nonzero(~clear)
    y0, y1, x0, x1 = max(0, ys.min() - 2), ys.max() + 3, max(0, xs.min() - 2), xs.max() + 3
    crop = clear[y0:y1, x0:x1]
    enclosed = crop & ~_reached(np, crop)
    if not enclosed.any():
        return out
    big = _morph(np, enclosed, 5, grow=False)  # the parts at least 5 px across ...
    for _ in range(80):  # ... and all of the region each belongs to
        grown = _morph(np, big, 5, grow=True) & enclosed
        if (grown == big).all():
            break
        big = grown
    out[y0:y1, x0:x1] = enclosed & ~big
    return out


def pinholes(np, alpha) -> int:
    """How many pixels of the picture are specks (speck_mask)."""
    return int(speck_mask(np, alpha).sum())


def check_geometry(
    res: CaseResult,
    plan: rm_plan.Plan,
    params: dict,
    props: dict,
    frames: dict[int, dict],
    overlay_dir: Path,
    n: int,
    fps: float,
) -> dict[int, str]:
    """blank, ink, safe_area, words_clear, frame_change. Returns frame -> pixel hash."""
    np = _np()
    r = props["region"]
    H, W = props["height"], props["width"]
    hashes: dict[int, str] = {}
    prev_scene, prev_img = None, None
    widths: dict[tuple[int, int], list[float]] = defaultdict(list)
    for f in range(n):
        rec = frames.get(f) or {"u": [], "s": []}
        img = load_frame(overlay_dir, f)
        if img.shape[:2] != (H, W):
            res.fail("safe_area", f"frame {f}: size {img.shape[1]}x{img.shape[0]}, wanted {W}x{H}")
            continue
        alpha = img[..., 3]
        hashes[f] = hashlib.sha1(img.tobytes()).hexdigest()
        ink = alpha > 0
        if not rec["u"] and not rec["s"]:
            res.ran("blank")
            if ink.any():
                res.fail("blank", f"frame {f}: {int(ink.sum())} drawn pixels on a frame with nothing planned")
        res.ran("safe_area")
        if ink.any():
            ys, xs = np.nonzero(ink.any(axis=1))[0], np.nonzero(ink.any(axis=0))[0]
            x0, x1, y0, y1 = int(xs[0]), int(xs[-1]) + 1, int(ys[0]), int(ys[-1]) + 1
            if x0 < r["x0"] or x1 > r["x1"] or y0 < r["y0"] or y1 > r["y1"]:
                res.fail(
                    "safe_area",
                    f"frame {f}: ink x {x0}-{x1} y {y0}-{y1} leaves the box x {r['x0']}-{r['x1']} y {r['y0']}-{r['y1']}",
                )
        # ink where the scene says, and nowhere else
        solid = alpha > 128
        allowed = np.zeros((H, W), bool)
        for u in rec["u"]:
            if not u["box"]:
                res.ran("ink")
                res.fail("ink", f"frame {f}: unit {u['id']} has no element")
                continue
            x, y, w, h = u["box"]
            # outline, shadow, glow, extrusion: they stretch with the unit, by its larger axis when squashed
            axis = max(math.hypot(u["m"][0], u["m"][1]), math.hypot(u["m"][2], u["m"][3]), 0.2)
            grow = u["reach"] * axis + 6
            a0, b0 = max(0, int(x - grow)), max(0, int(y - grow))
            a1, b1 = min(W, int(math.ceil(x + w + grow))), min(H, int(math.ceil(y + h + grow)))
            allowed[b0:b1, a0:a1] = True
            if thin(u) >= MIN_SCALE and u["tx"].strip() and visible_share(u) >= 0.5:
                res.ran("ink")
                c0, d0 = max(0, int(x)), max(0, int(y))
                c1, d1 = min(W, int(math.ceil(x + w))), min(H, int(math.ceil(y + h)))
                if u["clip"]:  # through a window: the part of the box the window shows
                    cx, cy, cw, ch = u["clip"]
                    c0, d0 = max(c0, int(math.ceil(cx))), max(d0, int(math.ceil(cy)))
                    c1, d1 = min(c1, int(cx + cw)), min(d1, int(cy + ch))
                if c1 <= c0 or d1 <= d0 or int(solid[d0:d1, c0:c1].sum()) < 12:
                    res.fail(
                        "ink", f"frame {f}: unit {u['id']} {u['tx']!r} has no pixels in its box {u['box']}"
                    )
        for s in rec["s"]:
            if s["box"]:
                x, y, w, h = s["box"]
                allowed[
                    max(0, int(y) - 8) : min(H, int(y + h) + 9), max(0, int(x) - 8) : min(W, int(x + w) + 9)
                ] = True
        if rec["u"] or rec["s"]:
            res.ran("specks")
            gaps = speck_mask(np, alpha)
            holes = int(gaps.sum())
            if holes:
                ys, xs = np.nonzero(gaps)
                res.fail(
                    "specks",
                    f"frame {f}: {holes} px of video walled in by the caption, the first at x {int(xs[0])} y {int(ys[0])}",
                )
                res.stats["speck_frames"] = res.stats.get("speck_frames", 0) + 1
                res.stats["speck_px"] = res.stats.get("speck_px", 0) + holes
                if holes > res.stats.get("speck_px_max", 0):
                    res.stats["speck_px_max"], res.stats["speck_px_at"] = holes, f  # the frame to look at
        if rec["u"] or rec["s"]:
            res.ran("stray_ink")
            stray = int((solid & ~allowed).sum())
            if stray > 0:
                res.fail("stray_ink", f"frame {f}: {stray} solid pixels outside every unit and shape")
        # neighbours on a line keep clear of each other (boxes of the words as drawn)
        boxes = _unit_boxes(rec)
        by_page: dict[tuple[int, int], list[tuple[int, list[float]]]] = defaultdict(list)
        for (lb, pid, wi), b in boxes.items():
            by_page[(lb, pid)].append((wi, b))
        size = float(params["size"])
        # a leaning or turned word has a wider box than its ink: half its height times the lean, each side
        lean: dict[tuple[int, int, int], float] = defaultdict(float)
        for u in rec["u"]:
            if u["box"]:
                k = (u["lb"], u["p"], u["w"])
                slant = (abs(u["m"][1]) + abs(u["m"][2])) / max(math.hypot(u["m"][2], u["m"][3]), 0.2)
                lean[k] = max(lean[k], 0.5 * u["box"][3] * min(1.0, slant))
        for (_lb, pid), items in by_page.items():
            items.sort()
            for (i, a), (j, b) in zip(items, items[1:], strict=False):
                if j != i + 1:
                    continue
                same_line = min(a[3], b[3]) - max(a[1], b[1]) > 0.6 * min(a[3] - a[1], b[3] - b[1])
                if same_line and b[0] > a[0]:
                    res.ran("words_clear")
                    if a[2] - b[0] > 0.05 * size + lean[(_lb, pid, i)] + lean[(_lb, pid, j)]:
                        res.fail(
                            "words_clear",
                            f"frame {f}: page {pid} words {i} and {j} overlap by {a[2] - b[0]:.1f}px",
                        )
        # the drawn width of a resting word against the font file (a fallback font would not match)
        for u in rec["u"]:
            if (
                u["c"] < 0
                and not u["lb"]
                and u["box"]
                and abs(u["m"][0] - 1) < 0.002  # at rest: not scaled, squashed, turned or leaning
                and abs(u["m"][3] - 1) < 0.002
                and abs(u["rot"]) < 0.01
                and abs(u["m"][1]) < 1e-4
                and abs(u["m"][2]) < 1e-4
            ):
                widths[(u["p"], u["w"])].append(u["box"][2])
        scene = json.dumps([rec["u"], rec["s"]], sort_keys=True)
        if prev_scene is not None and f - 1 in hashes:
            res.ran("frame_change")
            if scene == prev_scene and hashes[f] != hashes[f - 1] and prev_img is not None:
                # the scene record is rounded (1/100 px): a spring's last hundredths may still nudge an edge
                seen = [
                    x[..., :3].astype(np.float32) * (x[..., 3:4].astype(np.float32) / 255)
                    for x in (img, prev_img)
                ]
                d = int(
                    max(
                        np.abs(seen[0] - seen[1]).max(),
                        np.abs(img[..., 3].astype(np.int16) - prev_img[..., 3]).max(),
                    )
                )
                if d > 40:
                    res.fail("frame_change", f"frame {f}: pixels changed by {d} levels, the scene did not")
            if scene != prev_scene and hashes[f] == hashes[f - 1] and _moved(frames.get(f - 1), rec):
                res.fail("frame_change", f"frame {f}: the scene moved, the pixels did not (stutter)")
        prev_scene, prev_img = scene, img
    pages = {p["id"]: p for p in props["pages"]}
    sizes = {pg["id"]: pg["size"] for pg in (res.stats.get("_pages") or [])}
    for (pid, wi), ws in widths.items():
        if pid not in pages or pid not in sizes:
            continue
        w = pages[pid]["words"][wi]
        scale = params["emphScale"] if w["emph"] else 1.0
        if any(ord(ch) in rm.emoji_set() for ch in w["text"]):
            continue
        font = params.get("emphFont") or params["font"] if w["emph"] else params["font"]
        want = rm_plan.ink_width(w["text"], font, sizes[pid] * scale)
        if want is None:
            continue
        want += float(params["letterSpacing"]) * sizes[pid] * scale * (len(w["text"]) - 1)  # tracking
        got = sorted(ws)[len(ws) // 2]
        res.ran("font")
        # the reference has no kerning (Pillow's plain layout): the drawn word may be up to 12 % tighter
        if want > 20 and (got - want > max(4.0, 0.06 * want) or want - got > max(4.0, 0.12 * want)):
            res.fail(
                "font",
                f"page {pid} word {wi} {w['text']!r}: drawn {got:.1f}px wide, the font file says {want:.1f}px",
            )
    return hashes


def _moved(a: dict | None, b: dict) -> bool:
    """True when some unit visibly differs between two scene records (more than a hundredth of a pixel)."""
    if a is None:
        return False

    def shown(rec: dict) -> list[dict]:  # a unit scaled to nothing draws no pixel: it cannot stutter
        return [u for u in rec["u"] if not (u["box"] and u["sc"] < 0.02)]

    ua = {u["id"]: u for u in shown(a)}
    for u in (ub := shown(b)):
        if u.get("em"):  # an emoji is system text, which the browser moves in whole-pixel steps
            continue
        o = ua.get(u["id"])
        if o is None or o["fg"] != u["fg"] or not o["box"] or not u["box"]:
            return True
        if any(abs(x - y) > 0.35 for x, y in zip(o["box"], u["box"], strict=True)):
            return True
    return len(ua) != len(ub)


def _series(frames: dict[int, dict], n: int) -> dict[str, dict[int, dict]]:
    out: dict[str, dict[int, dict]] = defaultdict(dict)
    for f in range(n):
        for u in (frames.get(f) or {"u": []})["u"]:
            if not u["box"] or u["sc"] < 0.02:  # scaled to nothing: it has no position or angle to follow
                continue
            x, y, w, h = u["box"]
            rgb = palette.parse_hex(u["fg"][0]) if u["fg"] else (0, 0, 0)
            out[u["id"]][f] = {"x": x + w / 2, "y": y + h / 2, "sc": u["sc"], "rot": u["rot"], "col": rgb}
    return out


def _delta(a: dict, b: dict, ch: str) -> float:
    if ch in "rgb":  # one colour channel, 8-bit levels
        i = "rgb".index(ch)
        return float(b["col"][i] - a["col"][i])
    d = b[ch] - a[ch]
    if ch == "rot":
        d = (d + 180) % 360 - 180
    return float(d)


def check_continuity(res: CaseResult, frames: dict[int, dict], n: int, fps: float) -> None:
    """jump: a change much larger than the changes around it. flash: a value that sits, leaves for one
    frame and is back (a blip), or that reverses direction on two frames running (a buzz: faster than the
    frame rate can show). The turning point of a bounce or of a spring's overshoot is neither: the value was
    already moving before it and keeps moving after. Thresholds are in pixels, scale units, degrees and 8-bit
    colour levels per frame."""
    k = 30.0 / fps  # the same motion moves half as far per frame at 60 fps
    for uid, ser in _series(frames, n).items():
        fs = sorted(ser)
        for i in range(1, len(fs) - 2):
            f0, f1, f2, f3 = fs[i - 1], fs[i], fs[i + 1], fs[i + 2]
            if f3 - f0 != 3:
                continue
            f4 = fs[i + 3] if i + 3 < len(fs) and fs[i + 3] == f3 + 1 else None
            for ch in ("x", "y", "sc", "rot", "r", "g", "b"):
                d1, d2, d3 = (_delta(ser[a], ser[b], ch) for a, b in ((f0, f1), (f1, f2), (f2, f3)))
                d4 = _delta(ser[f3], ser[f4], ch) if f4 is not None else 0.0
                res.ran("jump")
                if abs(d2) > 5 * max(abs(d1), abs(d3)) + EPS_JUMP[ch] * k:
                    res.fail(
                        "jump",
                        f"unit {uid} {ch}: jumps {d2:+.2f} at frame {f2} (before {d1:+.2f}, after {d3:+.2f})",
                    )
                res.ran("flash")
                eps = EPS_FLASH[ch]
                back = _delta(ser[f1], ser[f3], ch)
                out_and_back = abs(d2) > eps and abs(d3) > eps and abs(back) < 0.25 * abs(d2)
                blip = out_and_back and d2 * d3 < 0 and abs(d1) < 0.34 * abs(d2) and abs(d4) < 0.34 * abs(d3)
                buzz = min(abs(d1), abs(d2), abs(d3)) > eps and d1 * d2 < 0 and d2 * d3 < 0
                if blip:
                    res.fail("flash", f"unit {uid} {ch}: one-frame excursion {d2:+.2f} at frame {f2}")
                elif buzz:
                    res.fail(
                        "flash",
                        f"unit {uid} {ch}: reverses every frame around frame {f2} ({d1:+.2f}, {d2:+.2f}, {d3:+.2f})",
                    )


def _morph(np, mask, size: int, grow: bool):
    from PIL import Image, ImageFilter

    im = Image.fromarray((mask * 255).astype(np.uint8))
    flt = ImageFilter.MaxFilter(size) if grow else ImageFilter.MinFilter(size)
    return np.asarray(im.filter(flt)) > 127


RING_PCT = 2.0  # share (%) of the ring allowed to be anti-aliased corner pixels


def unit_masks(np, img, u: dict):
    """((y0, y1, x0, x1), core, ring) for a unit: `core` is the solid inside of its fill, `ring` the pixels
    2 to 4 px outside the fill's edge. None when the unit has no solid fill to measure (too small, clipped)."""
    x, y, w, h = u["box"]
    H, W = img.shape[:2]
    pad = int(u["halo"] * max(u["sc"], thin(u))) + 8
    x0, y0 = max(0, int(x) - pad), max(0, int(y) - pad)
    x1, y1 = min(W, int(math.ceil(x + w)) + pad), min(H, int(math.ceil(y + h)) + pad)
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    ov = img[y0:y1, x0:x1].astype(np.int16)
    fg = np.zeros(ov.shape[:2], bool)
    cols = [palette.parse_hex(c) for c in u["fg"]]
    pts = list(cols)
    for a, b in zip(cols, cols[1:], strict=False):  # a gradient: the colours between its stops count too
        pts += [tuple(round(a[i] + (b[i] - a[i]) * t / 6) for i in range(3)) for t in range(1, 6)]
    opaque = ov[..., 3] >= 250
    for c in pts:
        fg |= (np.abs(ov[..., :3] - np.array(c, np.int16)).max(axis=-1) <= 14) & opaque
    # only inside the unit's own ink box: a neighbour (or an emoji) of a similar colour is not this fill.
    # It is not what the fill is read against either: `alike` (the same colour anywhere near) stays out of
    # the ring, so the next letter of the same word, a hair away in its own unit, is not a contrast failure
    alike = fg.copy()
    own = np.zeros(ov.shape[:2], bool)
    own[
        max(0, int(y) - 1 - y0) : int(math.ceil(y + h)) + 1 - y0,
        max(0, int(x) - 1 - x0) : int(math.ceil(x + w)) + 1 - x0,
    ] = True
    fg &= own
    win = None
    if u["clip"]:  # a unit shown through a window: only what is inside the window is this unit
        cx, cy, cw, ch = u["clip"]
        win = np.zeros(ov.shape[:2], bool)
        a0, b0 = max(0, int(math.ceil(cx)) + 2 - x0), max(0, int(math.ceil(cy)) + 2 - y0)
        a1, b1 = int(cx + cw) - 2 - x0, int(cy + ch) - 2 - y0
        if a1 > a0 and b1 > b0:
            win[b0:b1, a0:a1] = True
        fg = fg & win
    core = _morph(np, fg, 3, grow=False)
    if int(core.sum()) < 24:
        return None
    reach = u["halo"] * thin(u)
    # dilation window: the ring is the pixels 2 to 4 px outside the fill under a full outline, 2 to 3 px
    # under a thinner one or none (text on a plate): what is there is what the fill is read against
    outer = 9 if reach >= 5 else 7
    ring = _morph(np, core, outer, grow=True) & ~_morph(np, alike, 3, grow=True)
    ring &= ~_slivers(np, ov, pts, opaque, alike)
    if win is not None:
        ring &= win
    if int(ring.sum()) < 12:
        return None
    return (y0, y1, x0, x1), core, ring


def _slivers(np, ov, colours, opaque, alike):
    """What is left of a letter while a shape slides over it can be under two pixels wide for a frame: no
    pixel of it has the fill's full colour, so it is not `alike`, yet it is the letter and not what the
    letter is read against. The shades of the fill (the fill dimmed towards black, at least half of it
    left) in strips under 3 px wide that touch the fill; a wider light area beside the fill is not one."""
    rgb = ov[..., :3].astype(np.float32)
    shade = np.zeros(ov.shape[:2], bool)
    for c in colours:
        cv = np.array(c, np.float32)
        if float(cv @ cv) < 1:
            continue  # black has no shades
        s = (rgb @ cv) / float(cv @ cv)
        shade |= opaque & (s >= 0.5) & (s < 1) & (np.abs(rgb - s[..., None] * cv).max(axis=-1) <= 14)
    shade &= ~alike
    strip = shade & ~_morph(np, _morph(np, shade, 3, grow=False), 3, grow=True)
    sliver = strip & _morph(np, alike, 3, grow=True)
    for _ in range(12):
        grown = _morph(np, sliver, 3, grow=True) & strip
        if (grown == sliver).all():
            break
        sliver = grown
    return sliver


def masks_contrast(np, ov, bg, core, ring) -> float:
    """WCAG contrast between the fill (core) and the ring around it, composited over bg (None = the
    overlay's own pixels, for a ring that is fully opaque): the darkest fill pixel against the lightest
    ring pixel (or the reverse for dark text), leaving out RING_PCT of the ring for anti-aliased corners."""

    def lum_at(mask):
        px = ov[mask].astype(np.float64)
        if bg is None:
            return _lum(np, px[:, :3])
        a = px[:, 3:4] / 255.0
        return _lum(np, np.rint(bg[mask].astype(np.float64) * (1 - a) + px[:, :3] * a))

    lf, lr = lum_at(core), lum_at(ring)
    if float(np.median(lf)) >= float(np.median(lr)):
        return (float(lf.min()) + 0.05) / (float(np.percentile(lr, 100 - RING_PCT)) + 0.05)
    return (float(np.percentile(lr, RING_PCT)) + 0.05) / (float(lf.max()) + 0.05)


def composite(np, img, bg):
    a = img[..., 3:4].astype(np.float64) / 255.0
    return np.clip(
        np.rint(bg.astype(np.float64) * (1 - a) + img[..., :3].astype(np.float64) * a), 0, 255
    ).astype(np.uint8)


def check_contrast(
    res: CaseResult, frames: dict[int, dict], overlay_dir: Path, n: int, backgrounds, name: str = "contrast"
) -> None:
    """Every fill of every frame against what is next to it, over each background. `backgrounds` is a list
    of (label, fn(frame, y0, y1, x0, x1) -> (y1 - y0, x1 - x0, 3) uint8)."""
    np = _np()
    lowest = res.stats.get(f"{name}_min", float("inf"))
    below_target = 0
    for f in range(n):
        rec = frames.get(f)
        if not rec or not rec["u"]:
            continue
        img = load_frame(overlay_dir, f)
        for u in rec["u"]:
            if not u["box"] or not u["fg"]:
                continue
            if thin(u) < MIN_SCALE:
                res.checks[f"{name}_in_transition"] += 1
                continue
            got = unit_masks(np, img, u)
            if got is None:
                res.checks[f"{name}_unmeasurable"] += 1
                continue
            (y0, y1, x0, x1), core, ring = got
            ov = img[y0:y1, x0:x1]
            # an opaque ring (a full outline or a plate) looks the same over any background: measure once
            opaque = bool((ov[..., 3][ring] == 255).all()) and bool((ov[..., 3][core] == 255).all())
            same = masks_contrast(np, ov, None, core, ring) if opaque else None
            for label, fn in backgrounds:
                cr = same if same is not None else masks_contrast(np, ov, fn(f, y0, y1, x0, x1), core, ring)
                res.ran(name)
                lowest = min(lowest, cr)
                if cr < palette.TARGET:
                    below_target += 1
                if cr < palette.FLOOR:
                    res.fail(
                        name,
                        f"frame {f} over {label}: unit {u['id']} {u['tx']!r} fill {u['fg']} has {cr:.2f}:1 against the pixels next to it",
                    )
    res.stats[f"{name}_min"] = lowest if lowest != float("inf") else 0.0
    res.stats[f"{name}_below_target"] = res.stats.get(f"{name}_below_target", 0) + below_target


def check_fps_pair(a: CaseResult, fa: dict[int, dict], fb: dict[int, dict], n30: int) -> None:
    """The 30 fps render against the 60 fps render of the same case: frame k at 30 fps sits in time exactly
    between frames 2k and 2k+1 at 60 fps, so its values must lie between theirs (plus a small margin)."""
    s30, s60 = _series(fa, n30), _series(fb, n30 * 2)
    tol = {"x": 3.0, "y": 3.0, "sc": 0.03, "rot": 1.2}
    for uid, ser in s30.items():
        other = s60.get(uid, {})
        for k, v in ser.items():
            p, q = other.get(2 * k), other.get(2 * k + 1)
            if not p or not q:
                continue
            for ch in ("x", "y", "sc", "rot"):
                a.ran("fps_match")
                lo, hi = min(p[ch], q[ch]), max(p[ch], q[ch])
                span = hi - lo
                # a turning point (the floor of a bounce) can fall between the two 60 fps frames: it reaches
                # past both by up to half of what the value moves in the frames around them
                before, after = other.get(2 * k - 1), other.get(2 * k + 2)
                around = max(
                    abs(p[ch] - before[ch]) if before else 0.0, abs(after[ch] - q[ch]) if after else 0.0
                )
                slack = tol[ch] + 0.6 * span + 0.5 * around
                if not (lo - slack <= v[ch] <= hi + slack):
                    a.fail(
                        "fps_match",
                        f"unit {uid} {ch} at 30fps frame {k}: {v[ch]:.2f}, 60fps has {p[ch]:.2f} and {q[ch]:.2f}",
                    )


# ------------------------------------------------------------------ ffmpeg composite


def check_composite(res: CaseResult, ov: rm.Overlay, work: Path, n: int, fps: float) -> None:
    """Lay the overlay over a moving synthetic video with the production filter and compare every frame, in
    the video's own YUV 4:2:0, with the exact composite (done in RGB with numpy, converted the same way).
    A wrong frame offset, premultiplied edges or a colour-matrix mix-up all show up as large differences."""
    from PIL import Image

    np = _np()
    H, W = ov.height, ov.width
    ff = config.ffmpeg_bin()
    bg_dir, ref_dir = work / "cmp_bg", work / "cmp_ref"
    for d in (bg_dir, ref_dir):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    for f in range(n):
        bg = background("blocks", f, H, W)
        Image.fromarray(bg).save(bg_dir / f"{f:06d}.png", compress_level=1)
        Image.fromarray(composite(np, load_frame(ov.dir, f), bg)).save(
            ref_dir / f"{f:06d}.png", compress_level=1
        )
    to_yuv = f"{rm.TO_YUV},format=yuv444p,scale=flags=area"  # as the production filter converts
    size = W * H * 3 // 2

    def run(args: list[str]) -> bytes:
        r = subprocess.run(
            [ff, "-v", "error", *args, "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
            capture_output=True,
            timeout=600,
        )
        if r.returncode != 0:
            raise rm.RemotionError(f"composite check: ffmpeg failed: {r.stderr[-400:]!r}")
        return r.stdout

    seq = ["-framerate", f"{fps:g}", "-start_number", "0", "-i"]
    ref = run([*seq, str(ref_dir / "%06d.png"), "-vf", f"{to_yuv},format=yuv420p"])
    twin = rm.Overlay(dir=ov.dir, pattern=ov.pattern, fps=fps, frames=n, width=W, height=H, x=0, y=0)
    graph = f"[0:v]{to_yuv},format=yuv420p[base];" + rm.overlay_filter(1, twin, "[base]", "[v]")
    got = run(
        [*seq, str(bg_dir / "%06d.png"), *rm.overlay_input(twin), "-filter_complex", graph, "-map", "[v]"]
    )
    res.ran("composite")
    if len(got) != len(ref) or len(ref) != size * n:
        res.fail(
            "composite",
            f"{len(got) // size} composited frames, {len(ref) // size} reference frames, {n} wanted",
        )
        return
    a = np.frombuffer(ref, np.uint8).reshape(n, size).astype(np.int16)
    b = np.frombuffer(got, np.uint8).reshape(n, size).astype(np.int16)
    worst = 0
    for f in range(n):
        res.ran("composite")
        d = np.abs(a[f] - b[f])
        y, c = int(d[: W * H].max()), int(d[W * H :].max())
        worst = max(worst, y, c)
        if y > 3 or c > 4:
            res.fail(
                "composite",
                f"frame {f}: ffmpeg composite differs from the exact one by {y} (luma) / {c} (chroma) levels",
            )
    res.stats["composite_max_diff"] = worst
    for d in (bg_dir, ref_dir):
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ running a case


def run_case(
    template: str, fixture: Fixture | str, fps: float, work: Path, *, colors: str = "fixed",
    synth: tuple[str, ...] = SYNTH, own_colours: bool = True, deterministic: bool = False,
    composite_check: bool = False, keep: bool = False, bg_video=None, words: list[Word] | None = None,
    dur: float | None = None, box_kind: str | None = None, params: dict | None = None, pixels: bool = True,
    raw: bool = False,
) -> tuple[CaseResult, dict[int, dict]]:  # fmt: skip
    """Render one case with every frame kept and check it. colors "fixed" checks contrast over the
    synthetic backgrounds; "auto" needs bg_video = (label, Background, fn(frame) -> pixels) and checks
    contrast over that. pixels=False draws nothing: every frame is laid out and only the checks that read
    the scene run (plan, highlight, layout, jump, flash, lifetime), in a fraction of the time. raw=True
    takes the frames as the browser drew them, before the renderer closes the gaps in them (to show that
    `specks` sees one). Returns the result and the per-frame scene records (for fps pairs)."""
    fx = FIXTURES[fixture] if isinstance(fixture, str) else fixture
    wl = words if words is not None else fx.word_list()
    dur = dur if dur is not None else fx.dur
    res = CaseResult(template, fx.name, fps, colors)
    P = params if params is not None else rm.load_template(template)
    box = rm.caption_box(
        {"seam": 960, "frame": 1450, "reframe": 1250, "stack": 1495}[box_kind or fx.box], box_kind or fx.box
    )
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    props, plan = rm.build_props(
        wl, dur, template, box=box, fps=fps, beats=list(fx.beats) if words is None else None,
        actions=list(fx.actions) if words is None else None, colors=colors,
        background=bg_video[1] if bg_video else None, params=P, pad=PAD, debug=True,
    )  # fmt: skip
    out = work / "frames"
    shutil.rmtree(out, ignore_errors=True)
    t0 = time.monotonic()
    ov = rm.render_props(
        props, out, y=box.y0 - PAD, debug=work / "scene.jsonl", all_frames=True, scene_only=not pixels, raw=raw
    )  # fmt: skip
    res.render_s = time.monotonic() - t0
    n = ov.frames
    res.frames = n
    if pixels and not raw:  # what the renderer's last step had to close: the specks there were before it
        res.stats["gaps_closed_frames"], res.stats["gaps_closed_px"] = ov.gap_frames, ov.gap_px
    init, frames = load_debug(work / "scene.jsonl")
    res.stats["_pages"] = init.get("pages", [])
    check_layout(res, init, P, props)
    check_plan_frames(res, plan, P, frames, n, fps)
    check_highlight(res, plan, frames, fps)
    hashes = check_geometry(res, plan, P, props, frames, out, n, fps) if pixels else {}
    check_continuity(res, frames, n, fps)
    if not pixels:
        res.stats["scene_only"] = 1
    elif bg_video:
        check_contrast(
            res, frames, out, n, [(bg_video[0], lambda f, y0, y1, x0, x1: bg_video[2](f)[y0:y1, x0:x1])]
        )
    else:
        kinds = list(synth)
        if own_colours:  # the adversarial case: the caption's own fill colours behind it
            kinds += sorted(
                {P["colors"][k] for k in ("text", "active", "emph")} | set(P["colors"]["cycle"][:2])
            )
        check_contrast(
            res, frames, out, n,
            [(k, (lambda f, y0, y1, x0, x1, k=k: background(k, f, y1 - y0, x1 - x0, y0, x0))) for k in kinds],
        )  # fmt: skip
    if deterministic and pixels:
        again = work / "frames2"
        shutil.rmtree(again, ignore_errors=True)
        rm.render_props(props, again, y=box.y0 - PAD, all_frames=True, raw=raw)
        for f in range(n):
            res.ran("deterministic")
            if hashlib.sha1(load_frame(again, f).tobytes()).hexdigest() != hashes.get(f):
                res.fail("deterministic", f"frame {f} differs between two renders of the same props")
        shutil.rmtree(again, ignore_errors=True)
    if composite_check and pixels:
        check_composite(res, ov, work, n, fps)
    res.stats.pop("_pages", None)
    res.stats["plan_pages"] = len(plan.pages)
    res.stats["plan_labels"] = len(plan.labels)
    if not keep:
        shutil.rmtree(out, ignore_errors=True)
    return res, frames


class VideoBand:
    """The rows of a 1080x1920 video that the check canvas covers, frame by frame, decoded once in order."""

    def __init__(self, video: Path, y0: int, y1: int, fps: float):
        if y0 < 0 or y1 > rm.FRAME_H:
            raise ValueError("the check canvas reaches outside the video")
        self.y0, self.h = y0, y1 - y0
        cmd = [
            config.ffmpeg_bin(), "-v", "error", "-i", str(video), "-an",
            "-vf", f"fps={fps:g},crop={rm.FRAME_W}:{self.h}:0:{self.y0}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]  # fmt: skip
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.at, self.frame = -1, None

    def __call__(self, f: int):
        np = _np()
        size = rm.FRAME_W * self.h * 3
        while self.at < f:
            raw = self.proc.stdout.read(size)
            if len(raw) < size:  # the video ended: hold its last frame
                if self.frame is None:
                    self.frame = np.zeros((self.h, rm.FRAME_W, 3), np.uint8)
                self.at = f
                break
            self.frame = np.frombuffer(raw, np.uint8).reshape(self.h, rm.FRAME_W, 3)
            self.at += 1
        return self.frame

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()


def video_seconds(video: Path) -> tuple[float, float]:
    """(duration, frame rate) of a video."""
    r = subprocess.run(
        [config.ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate:format=duration", "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    info = json.loads(r.stdout)
    n, _, d = info["streams"][0]["r_frame_rate"].partition("/")
    return float(info["format"]["duration"]), float(n) / float(d or 1)


def run_video_case(
    template: str, video: Path, work: Path, *, kind: str = "seam", colors: str = "auto", seed: int = 7,
    limit_s: float | None = None, keep: bool = False,
) -> CaseResult:  # fmt: skip
    """Every frame of a real 1080x1920 clip: made-up word timings for its whole length, colours picked for
    its own picture (auto) or the fixed scheme, all the checks, and the contrast of every fill against the
    clip's real pixels."""
    video = Path(video)
    dur, fps = video_seconds(video)
    dur = min(dur, limit_s) if limit_s else dur
    fps = min(fps, 60.0)
    cap_y = {"seam": 960, "frame": 1450, "reframe": 1250, "stack": 1495}[kind]
    box = rm.caption_box(cap_y, kind)
    bg = rm.sample_video(video, box)
    band = VideoBand(video, box.y0 - PAD, box.y1 + PAD, fps)
    fx = Fixture(f"{video.parent.name[:28]}:{kind}", dur, (), box=kind)
    try:
        res, _ = run_case(
            template, fx, fps, work, colors=colors, bg_video=(video.parent.name, bg, band), keep=keep,
            words=pseudo_transcript(dur, seed), dur=dur,
        )  # fmt: skip
    finally:
        band.close()
    return res


def run_template(
    template: str,
    work: Path,
    *,
    fixtures=QUICK,
    fps_list=(30.0, 60.0),
    extras: bool = True,
    keep: bool = False,
) -> list[CaseResult]:
    """Every fixture at every frame rate for one template, with the 30/60 comparison. With extras, the first
    fixture is also rendered twice (determinism) and composited with ffmpeg."""
    out: list[CaseResult] = []
    for i, fx in enumerate(fixtures):
        recs = {}
        for fps in fps_list:
            res, frames = run_case(
                template, fx, fps, Path(work) / f"{template}-{fx}-{fps:g}", deterministic=extras and i == 0 and fps == fps_list[0],
                composite_check=extras and i == 0, keep=keep,
            )  # fmt: skip
            recs[fps] = (res, frames)
            out.append(res)
        if 30.0 in recs and 60.0 in recs:
            check_fps_pair(recs[30.0][0], recs[30.0][1], recs[60.0][1], recs[30.0][0].frames)
    return out


def run_sweep(
    template: str,
    work: Path,
    *,
    fixtures=SWEEP,
    phases=PHASES,
    fps_list=(SWEEP_FPS,),
    pixels: bool = True,
    keep: bool = False,
) -> list[CaseResult]:
    """The phase sweep for one template: every fixture again with its words moved by each of `phases` (a
    fraction of a frame at SWEEP_FPS), so every start, end and turn of the motion is sampled at several
    instants of the frame. A motion that is one function of time passes at all of them or has a change
    that fits into a single frame. With both 30 and 60 in fps_list the two are compared at every phase."""
    out: list[CaseResult] = []
    for fx in fixtures:
        for ph in phases:
            case = FIXTURES[fx].shifted(ph, SWEEP_FPS)
            recs = {}
            for fps in fps_list:
                res, frames = run_case(
                    template,
                    case,
                    fps,
                    Path(work) / f"{template}-{case.name}-{fps:g}",
                    pixels=pixels,
                    keep=keep,
                )
                res.stats["phase"] = ph
                recs[fps] = (res, frames)
                out.append(res)
            if 30.0 in recs and 60.0 in recs:
                check_fps_pair(recs[30.0][0], recs[30.0][1], recs[60.0][1], recs[30.0][0].frames)
    return out


def parse_phases(text: str) -> tuple[float, ...]:
    """ "4" -> four phases evenly over the frame (0, 0.25, 0.5, 0.75); "0,0.3,0.9" -> those."""
    parts = [x for x in text.split(",") if x.strip()]
    if len(parts) == 1 and float(parts[0]) >= 1:
        n = int(float(parts[0]))
        return tuple(round(i / n, 6) for i in range(n))
    out = tuple(float(x) for x in parts)
    if not out or any(not 0 <= x < 1 for x in out):
        raise ValueError(f"phases are fractions of a frame, 0 <= phase < 1: {text!r}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Frame-by-frame checks of the Remotion caption templates.")
    ap.add_argument("--templates", default="all", help="comma-separated names, or all")
    ap.add_argument(
        "--fixtures", default=None, help="quick | full | comma-separated fixture names (default: full)"
    )
    ap.add_argument("--fps", default=None, help="frame rates (default 30,60; the phase sweep: 30)")
    ap.add_argument(
        "--phases",
        help="the phase sweep instead of the plain run: the words moved by these fractions of a 30 fps "
        "frame (0,0.25,0.5,0.75), or a count (8 = eight phases evenly over the frame); fixtures default "
        f"to {','.join(SWEEP)}",
    )
    ap.add_argument(
        "--scene-only",
        action="store_true",
        help="draw nothing: only the checks that read the scene (plan, highlight, jump, flash, lifetime)",
    )
    ap.add_argument("--work", required=True, help="scratch folder for frames")
    ap.add_argument("--report", help="write a JSON report here")
    ap.add_argument("--keep", action="store_true", help="keep the rendered frames")
    ap.add_argument(
        "--no-extras", action="store_true", help="skip the determinism and ffmpeg composite checks"
    )
    a = ap.parse_args(argv)
    why = rm.missing()
    if why:
        print(f"Remotion is not set up: {why}", file=sys.stderr)
        return 2
    names = rm.templates() if a.templates == "all" else a.templates.split(",")
    phases = parse_phases(a.phases) if a.phases else None
    named = a.fixtures or ("sweep" if phases else "full")
    fixtures = {"quick": QUICK, "full": FULL, "sweep": SWEEP}.get(named) or tuple(named.split(","))
    fps_list = tuple(float(x) for x in (a.fps or ("30" if phases else "30,60")).split(","))
    results: list[CaseResult] = []
    for name in names:
        t0 = time.monotonic()
        if phases:
            rs = run_sweep(
                name, Path(a.work), fixtures=fixtures, phases=phases, fps_list=fps_list,
                pixels=not a.scene_only, keep=a.keep,
            )  # fmt: skip
        elif a.scene_only:
            rs = [
                run_case(name, fx, fps, Path(a.work) / f"{name}-{fx}-{fps:g}", pixels=False)[0]
                for fx in fixtures
                for fps in fps_list
            ]
        else:
            rs = run_template(
                name, Path(a.work), fixtures=fixtures, fps_list=fps_list, extras=not a.no_extras, keep=a.keep
            )
        results += rs
        bad = [r for r in rs if not r.ok]
        print(
            f"{name}: {sum(r.frames for r in rs)} frames, {sum(r.total_checks for r in rs)} checks, {sum(r.total_fails for r in rs)} failures ({time.monotonic() - t0:.0f}s)",
            flush=True,
        )
        for r in bad:
            print(r.summary(), flush=True)
    total = {
        "frames": sum(r.frames for r in results),
        "checks": sum(r.total_checks for r in results),
        "failures": sum(r.total_fails for r in results),
    }
    if phases:
        total["phases"] = list(phases)
    print(json.dumps(total))
    if a.report:
        Path(a.report).write_text(
            json.dumps({"total": total, "cases": [r.to_json() for r in results]}, indent=1)
        )
    return 0 if total["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
