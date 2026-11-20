"""Caption colours: the curated palette, contrast maths and the "auto" colour mode.

Standards applied (see remotion/README.md for the full rules):

- WCAG 2.2 contrast ratio (L1 + 0.05) / (L2 + 0.05) with relative luminance from linearised sRGB, as in
  technique G18 (https://www.w3.org/WAI/WCAG22/Techniques/general/G18). Captions are "large-scale text",
  for which WCAG asks 3:1 (SC 1.4.3, AA) and 4.5:1 (SC 1.4.6, AAA). We hold a stricter line: FLOOR 4.5:1
  on every frame, TARGET 7:1 for every colour pair a template ships with. Ratios are never rounded
  ("4.499:1 would not meet the 4.5:1 threshold", Understanding SC 1.4.3).
- The contrast that counts is between a glyph and the pixels right next to it (G18 procedure: "the
  background pixels immediately next to the letter"). A wide outline around the letters "acts as a halo and
  would be considered background" (Understanding SC 1.4.3), so every template keeps an outline or a plate
  next to its fills and the ratio is measured against that; the video behind decides the hues.
- APCA lightness contrast Lc (APCA-W3 0.0.98G-4g constants) is computed as a second opinion; nothing is
  gated on it.

There is no published standard for which caption colours hold attention (vendor claims only). The palette
is the convention of the big clip channels: white base, one saturated highlight (yellow, green, cyan...)
and a warm emphasis colour, all light enough to clear 7:1 on a black outline.

Pure Python: no numpy, no network, no ffmpeg in this module.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

RGB = tuple[int, int, int]

FLOOR = 4.5  # WCAG contrast every fill keeps against what is next to it, on every frame
TARGET = 7.0  # what the shipped colour pairs are designed to
HUE_CLEAR = 38.0  # degrees of OKLCh hue a highlight must keep from a dominant hue under it
HUE_APART = 35.0  # degrees two roles need between them to be told apart (yellow and orange are 38 apart)
CHROMA_MIN = 0.045  # OKLCh chroma under which a pixel counts as grey (no hue to collide with)
COLLIDE_SHARE = 0.18  # share of the pixels under the caption that makes a hue "dominant"
STICKY = 0.35  # score bonus for keeping the colour of the page before (stability)
DWELL_S = 2.0  # a role that just changed keeps its colour at least this long unless it now collides

# The curated palette. Fills are light (relative luminance >= 0.30: 7:1 and more on black), inks are dark.
PALETTE: dict[str, str] = {
    "white": "#FFFFFF",
    "cream": "#FFF3D6",
    "yellow": "#FFE600",
    "lime": "#8DFF2F",
    "mint": "#3DFFC0",
    "cyan": "#00E5FF",
    "sky": "#6CC8FF",
    "violet": "#B9A0FF",
    "pink": "#FF70CB",
    "red": "#FF6B5E",
    "orange": "#FF9A1F",
    "gold": "#FFC53D",
    "black": "#000000",
    "ink": "#0B0B12",
    "navy": "#0A1238",
    "plum": "#240A33",
    "forest": "#052B1B",
    "maroon": "#3A0710",
}
LIGHT = ("white", "cream", "yellow", "lime", "mint", "cyan", "sky", "violet", "pink", "red", "orange", "gold")
DARK = ("black", "ink", "navy", "plum", "forest", "maroon")

ROLES = ("text", "active", "emph", "stroke", "shadow", "plate", "plateText", "accent", "accent2")


# ------------------------------------------------------------------ sRGB, luminance, contrast


def parse_hex(h: str) -> RGB:
    s = h.strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(f"colour must be #RRGGBB: {h!r}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def to_hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{min(255, max(0, round(v))):02X}" for v in rgb[:3])


def resolve(name_or_hex: str) -> str:
    """A palette name or a #RRGGBB value -> #RRGGBB (upper case)."""
    if name_or_hex in PALETTE:
        return PALETTE[name_or_hex]
    return to_hex(parse_hex(name_or_hex))


def _lin(v: float) -> float:
    s = v / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def _unlin(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return 255.0 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055)


def luminance(c: str | Sequence[float]) -> float:
    """WCAG 2.2 relative luminance, 0 (black) to 1 (white)."""
    r, g, b = parse_hex(c) if isinstance(c, str) else c[:3]
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast_lum(l1: float, l2: float) -> float:
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def contrast(a: str | Sequence[float], b: str | Sequence[float]) -> float:
    """WCAG 2.2 contrast ratio, 1 to 21. Not rounded."""
    return contrast_lum(luminance(a), luminance(b))


def apca(text: str | Sequence[float], bg: str | Sequence[float]) -> float:
    """APCA lightness contrast Lc of text on bg (APCA-W3 0.0.98G-4g). Positive: dark text on light."""

    def y(c) -> float:
        r, g, b = parse_hex(c) if isinstance(c, str) else c[:3]
        v = 0.2126729 * (r / 255) ** 2.4 + 0.7151522 * (g / 255) ** 2.4 + 0.0721750 * (b / 255) ** 2.4
        return v + (0.022 - v) ** 1.414 if v < 0.022 else v

    yt, yb = y(text), y(bg)
    if abs(yb - yt) < 0.0005:
        return 0.0
    if yb > yt:  # dark text on a light background
        s = (yb**0.56 - yt**0.57) * 1.14
        return 0.0 if s < 0.1 else (s - 0.027) * 100
    s = (yb**0.65 - yt**0.62) * 1.14
    return 0.0 if s > -0.1 else (s + 0.027) * 100


# ------------------------------------------------------------------ OKLab / OKLCh


def oklab(c: str | Sequence[float]) -> tuple[float, float, float]:
    r, g, b = parse_hex(c) if isinstance(c, str) else c[:3]
    lr, lg, lb = _lin(r), _lin(g), _lin(b)
    l_ = (0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb) ** (1 / 3)
    m_ = (0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb) ** (1 / 3)
    s_ = (0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb) ** (1 / 3)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def from_oklab(lab: Sequence[float]) -> tuple[float, float, float]:
    L, a, b = lab
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        _unlin(4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_),
        _unlin(-1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_),
        _unlin(-0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_),
    )


def oklch(c: str | Sequence[float]) -> tuple[float, float, float]:
    """(lightness 0..1, chroma, hue in degrees 0..360)."""
    L, a, b = oklab(c)
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360.0


def hue_distance(h1: float, h2: float) -> float:
    d = abs(h1 - h2) % 360.0
    return 360.0 - d if d > 180.0 else d


def mix(a: str, b: str, t: float) -> str:
    """Colour between a and b in OKLab, quantised to 256 steps: the renderer's mix() exactly."""
    if t <= 0 or a.upper() == b.upper():
        return a.upper()
    if t >= 1:
        return b.upper()
    k = round(t * 255) / 255
    la, lb = oklab(a), oklab(b)
    return to_hex(from_oklab([la[i] + (lb[i] - la[i]) * k for i in range(3)]))


def worst_between(a: str, b: str, against: str, steps: int = 32) -> float:
    """Lowest contrast against `against` of any colour on the eased path from a to b."""
    return min(contrast(mix(a, b, i / steps), against) for i in range(steps + 1))


# ------------------------------------------------------------------ the video under the caption


@dataclass
class BgStats:
    """What the video looks like under the caption box while one phrase is on screen."""

    lum_lo: float = 0.0  # 5th percentile of relative luminance
    lum_hi: float = 0.0  # 95th percentile
    lum_mean: float = 0.0
    chromatic: float = 0.0  # share of pixels with a hue at all
    hues: list[tuple[float, float]] = field(
        default_factory=list
    )  # (hue, share of all pixels), 15 degree bins
    samples: int = 0

    def share_near(self, hue: float, within: float = HUE_CLEAR) -> float:
        """Share of the pixels whose hue is within `within` degrees of `hue`."""
        return sum(w for h, w in self.hues if hue_distance(h, hue) <= within)


HUE_BINS = 24


def bg_stats(pixels: Iterable[Sequence[int]]) -> BgStats:
    """Statistics of an iterable of (r, g, b) pixels (the caption box over a phrase's time span)."""
    lums: list[float] = []
    bins = [0.0] * HUE_BINS
    n = 0
    chroma_n = 0
    cache: dict[tuple[int, int, int], tuple[float, float, float]] = {}
    for p in pixels:
        key = (p[0] >> 2, p[1] >> 2, p[2] >> 2)  # 64 levels a channel is plenty for statistics
        got = cache.get(key)
        if got is None:
            rgb = (key[0] * 4 + 2, key[1] * 4 + 2, key[2] * 4 + 2)
            _, c, h = oklch(rgb)
            got = cache[key] = (luminance(rgb), c, h)
        lums.append(got[0])
        n += 1
        if got[1] >= CHROMA_MIN:
            chroma_n += 1
            bins[int(got[2] / (360 / HUE_BINS)) % HUE_BINS] += 1.0
    if n == 0:
        return BgStats()
    lums.sort()
    width = 360 / HUE_BINS
    return BgStats(
        lum_lo=lums[int(0.05 * (n - 1))],
        lum_hi=lums[int(0.95 * (n - 1))],
        lum_mean=sum(lums) / n,
        chromatic=chroma_n / n,
        hues=[((i + 0.5) * width, b / n) for i, b in enumerate(bins) if b > 0],
        samples=n,
    )


# ------------------------------------------------------------------ the auto mode


def check_scheme(colors: dict, pairs: Sequence[Sequence[str]], floor: float = FLOOR) -> list[str]:
    """Problems of a colour scheme: every pair of adjacent roles must reach the floor."""
    out = []
    for a, b in pairs:
        for ca in _as_list(colors[a]):
            for cb in _as_list(colors[b]):
                r = contrast(ca, cb)
                if r < floor:
                    out.append(f"{a} {ca} on {b} {cb}: {r:.2f}:1 < {floor}:1")
    return out


def _as_list(v) -> list[str]:
    return list(v) if isinstance(v, (list, tuple)) else [v]


def collides(color: str, stats: BgStats) -> float:
    """How much of the video under the caption has this colour's hue (0 = none). Greys never collide."""
    _, c, h = oklch(color)
    if c < CHROMA_MIN:
        return 0.0
    return stats.share_near(h)


@dataclass
class AutoState:
    """What the auto mode remembers from page to page: the colours in use and when each last changed."""

    colors: dict | None = None
    changed_at: dict[str, float] = field(default_factory=dict)


def auto_colors(
    fixed: dict, auto: dict, stats: BgStats, state: AutoState | None = None, now: float = 0.0
) -> dict:
    """Colours for one phrase: the template's fixed scheme with its auto roles re-picked for the video.

    fixed: the template's own scheme (role -> #RRGGBB, plus "cycle"). auto: the template's rules
    (candidates per role, pairs that need contrast, roles that avoid the video's hues, roles that must
    differ). Roles without candidates keep their fixed colour, so the result is always a complete,
    designed scheme and never a random one.

    Per role, in the template's order, the candidate wins that (1) reaches the floor against every
    already-chosen role it is paired with, (2) is not the hue of a large part of the video under it,
    (3) differs in hue from the roles it must be told apart from, and then (4) comes first in the
    template's list, with a bonus for the colour the page before used. A role that changed less than
    DWELL_S ago keeps its colour unless it now collides: no flicker from page to page.
    """
    state = state if state is not None else AutoState()
    prev = state.colors or {}
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in fixed.items()}
    cands: dict[str, list[str]] = auto.get("candidates", {})
    pairs = [tuple(p) for p in auto.get("pairs", [])]
    avoid = set(auto.get("avoidHue", []))
    distinct = [tuple(p) for p in auto.get("distinct", [])]
    chosen: set[str] = {r for r in ROLES if r not in cands}

    def partners(role: str) -> list[str]:
        return [b if a == role else a for a, b in pairs if role in (a, b)]

    def rivals(role: str) -> list[str]:
        return [b if a == role else a for a, b in distinct if role in (a, b)]

    def score(role: str, color: str, rank: int) -> float | None:
        for other in partners(role):
            if other in chosen and any(contrast(color, o) < FLOOR for o in _as_list(out[other])):
                return None
        s = -0.12 * rank
        for other in partners(role):
            if other in chosen and all(contrast(color, o) >= TARGET for o in _as_list(out[other])):
                s += 0.05
        if role in avoid:
            share = collides(color, stats)
            s -= 6.0 * max(0.0, share - COLLIDE_SHARE * 0.5)
            if share >= COLLIDE_SHARE:
                s -= 3.0
        _, c, h = oklch(color)
        for other in rivals(role):
            if other in chosen:
                oc = out[other]
                _, c2, h2 = oklch(oc)
                if oc.upper() == color.upper():
                    s -= 5.0
                elif c >= CHROMA_MIN and c2 >= CHROMA_MIN and hue_distance(h, h2) < HUE_APART:
                    s -= 2.0
        if prev.get(role, "").upper() == color.upper():
            s += STICKY
        return s

    for role in [r for r in ROLES if r in cands]:
        options = [resolve(c) for c in cands[role]]
        scored = [(s, -i, c) for i, c in enumerate(options) if (s := score(role, c, i)) is not None]
        if not scored:  # nothing reaches the floor: keep the fixed colour, which the template was tested with
            chosen.add(role)
            continue
        best = max(scored)[2]
        keep = prev.get(role)
        if keep and keep.upper() != best.upper() and keep.upper() in (o.upper() for o in options):
            recent = now - state.changed_at.get(role, -1e9) < DWELL_S
            fine = (role not in avoid or collides(keep, stats) < COLLIDE_SHARE) and score(
                role, keep, 0
            ) is not None
            if recent and fine:
                best = keep
        out[role] = best.upper()
        chosen.add(role)

    if "cycle" in cands:  # per-word colours: drop the ones that collide, keep the template's order
        opts = [resolve(c) for c in cands["cycle"]]
        ok = [c for c in opts if collides(c, stats) < COLLIDE_SHARE]
        ok = [
            c for c in ok if all(contrast(c, o) >= FLOOR for p in partners("cycle") for o in _as_list(out[p]))
        ]
        out["cycle"] = (ok or opts)[:8]
        if (
            len(out["cycle"]) < 2 and len(opts) >= 2
        ):  # one colour is not a cycle: add the least-colliding rest
            rest = sorted((c for c in opts if c not in out["cycle"]), key=lambda c: collides(c, stats))
            out["cycle"] = (out["cycle"] + rest)[:2]

    for role in ROLES:
        if prev.get(role, "").upper() != str(out[role]).upper():
            state.changed_at[role] = now
    state.colors = {k: (list(v) if isinstance(v, list) else v) for k, v in out.items()}
    return out


def safe_transition(a: dict, b: dict, pairs: Sequence[Sequence[str]]) -> bool:
    """True when easing every role from scheme a to scheme b keeps all paired roles above the floor."""
    for x, y in pairs:
        if x == "cycle" or y == "cycle":
            continue
        for i in range(0, 9):
            t = i / 8
            if contrast(mix(a[x], b[x], t), mix(a[y], b[y], t)) < FLOOR:
                return False
    return True
