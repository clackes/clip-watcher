"""What a Remotion caption overlay shows and when: text cleaning, pages, timing. Pure Python.

The plan is the specification the renderer is checked against frame by frame (render/framecheck.py):

- A word is "active" (highlighted) from its start until the next word starts, or for `lingerS` into a pause.
- A page is on screen from its first word's start until the next page starts; before a pause it stays
  `holdS` after its last word and uses the end of that time for its exit animation. With continuous speech
  the next page replaces it with a cut (exit 0), or plays over it for `overlapS` when the template asks.
- Judge labels (*FAIL*, *WATCHING*) take the caption spot for their time: a word that starts while a label
  is up is not shown (the rule of the ASS `word` style); a word that started before keeps its place until
  the label arrives.
- Two words can never be active in the same frame: a word that starts less than MIN_ACTIVE_S after the
  one before is moved to that distance (only words with identical or near-identical timestamps).
"""

from __future__ import annotations

import functools
import struct
from dataclasses import dataclass, field
from pathlib import Path

from clipwatcher.models import Word
from clipwatcher.render.branding import FONTS_DIR
from clipwatcher.render.captions import _emphasis

LABEL_S = 0.9  # a beat label stays this long (captions.LABEL_S)
MIN_ACTIVE_S = 1 / 30  # every word is highlighted for at least one frame at 30 fps
MIN_LABEL_S = 0.25
MIN_EXIT_S = 0.1  # three frames at 30 fps
MIN_SHOWN_S = 0.1  # a word a label would cut off sooner than this is not shown at all
SHRINK_OK = 0.86  # a page may shrink the text to this share of its size before a word moves to the next page
KINDS = ("fail", "hype", "shock", "funny", "action")

_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "ʼ": "'", "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-", "…": "...", " ": " ",
}  # fmt: skip
_DROP = {0xFE0F, 0xFE0E, 0x200D, 0x200B, 0x200C, 0x2060, 0xFEFF} | set(range(0x1F3FB, 0x1F400))


# ------------------------------------------------------------------ fonts: coverage and widths


@functools.lru_cache(maxsize=64)
def font_chars(path: str) -> frozenset[int]:
    """Code points a TrueType/OpenType file (or the first font of a .ttc) can draw: cmap formats 4 and 12,
    read without any library and without loading the file (the system emoji font is 190 MB)."""
    with open(path, "rb") as fh:
        head = fh.read(16)
        base = struct.unpack_from(">I", head, 12)[0] if head[:4] == b"ttcf" else 0
        fh.seek(base)
        n = struct.unpack_from(">H", fh.read(12), 4)[0]
        table = None
        for _ in range(n):
            tag, _, off, length = struct.unpack(">4sIII", fh.read(16))
            if tag == b"cmap":
                table = (off, length)
        if table is None:
            return frozenset()
        fh.seek(table[0])
        data = fh.read(table[1])
    cmap = 0
    out: set[int] = set()
    for i in range(struct.unpack_from(">H", data, cmap + 2)[0]):
        plat, enc, off = struct.unpack_from(">HHI", data, cmap + 4 + 8 * i)
        if (plat, enc) not in ((0, 3), (0, 4), (0, 6), (3, 1), (3, 10)) and plat != 0:
            continue
        sub = cmap + off
        fmt = struct.unpack_from(">H", data, sub)[0]
        if fmt == 4:
            segs = struct.unpack_from(">H", data, sub + 6)[0] // 2
            ends = struct.unpack_from(f">{segs}H", data, sub + 14)
            starts = struct.unpack_from(f">{segs}H", data, sub + 16 + 2 * segs)
            deltas = struct.unpack_from(f">{segs}h", data, sub + 16 + 4 * segs)
            ro = sub + 16 + 6 * segs
            offs = struct.unpack_from(f">{segs}H", data, ro)
            for s in range(segs):
                for cp in range(starts[s], min(ends[s], 0xFFFE) + 1):
                    if offs[s] == 0:
                        gid = (cp + deltas[s]) & 0xFFFF
                    else:
                        g = struct.unpack_from(">H", data, ro + 2 * s + offs[s] + 2 * (cp - starts[s]))[0]
                        gid = (g + deltas[s]) & 0xFFFF if g else 0
                    if gid:
                        out.add(cp)
        elif fmt == 12:
            groups = struct.unpack_from(">I", data, sub + 12)[0]
            for g in range(groups):
                a, b, gid = struct.unpack_from(">III", data, sub + 16 + 12 * g)
                out.update(cp for cp in range(a, b + 1) if gid + (cp - a))
    return frozenset(out)


def font_path(spec: dict) -> Path:
    return FONTS_DIR / spec["file"]


@functools.lru_cache(maxsize=64)
def _pil_font(path: str):
    from PIL import ImageFont

    return ImageFont.truetype(path, 200)


EMOJI_EM = 1.0  # advance of an emoji in em (Apple Color Emoji)


def text_width(text: str, spec: dict, size: float, letter_spacing: float = 0.0) -> float:
    """Advance width of text in the font at `size` px (letter_spacing in em); characters the font does
    not have are emoji, one em each. Without the font file: an estimate, so planning never fails (the
    renderer measures for real and shrinks what does not fit)."""
    p = font_path(spec)
    if not p.exists():
        return len(text) * size * 0.6
    have = font_chars(str(p))
    f = _pil_font(str(p))
    own = "".join(ch for ch in text if ord(ch) in have)
    w = f.getlength(own) / 200.0 * size + (len(text) - len(own)) * EMOJI_EM * size
    return w + letter_spacing * size * len(own)


def ink_width(text: str, spec: dict, size: float) -> float | None:
    """Width of the ink of text (first to last drawn pixel column) at `size` px, None without the font."""
    p = font_path(spec)
    if not p.exists():
        return None
    have = font_chars(str(p))
    box = _pil_font(str(p)).getmask("".join(ch for ch in text if ord(ch) in have)).getbbox()
    return (box[2] - box[0]) / 200.0 * size if box else 0.0


@functools.lru_cache(maxsize=64)
def _cap_height(path: str) -> tuple[float, float, float]:
    """(cap height, ascender, descender) of the font's ink in em."""
    f = _pil_font(path)
    cap = -f.getbbox("HXE", anchor="ls")[1] / 200.0
    asc = -f.getbbox("bdfhkl", anchor="ls")[1] / 200.0
    desc = f.getbbox("gjpqy", anchor="ls")[3] / 200.0
    return cap, asc, desc


def ink_metrics(spec: dict) -> tuple[float, float, float]:
    p = font_path(spec)
    if not p.exists():
        return 0.72, 0.76, 0.22
    return _cap_height(str(p))


# ------------------------------------------------------------------ text


def emoji_chars(ttc: str | Path | None) -> frozenset[int]:
    """Emoji the system emoji font can draw: pictographs only (no digits, keycaps, flags, modifiers)."""
    if not ttc or not Path(ttc).is_file():
        return frozenset()
    try:
        have = font_chars(str(ttc))
    except (OSError, struct.error):
        return frozenset()
    return frozenset(
        cp for cp in have
        if cp >= 0x2190 and cp not in _DROP and not 0x1F1E6 <= cp <= 0x1F1FF and cp != 0x20E3 and cp < 0xE0000
    )  # fmt: skip


def split_emoji(text: str, emoji: frozenset[int]) -> list[str]:
    """A word with emoji attached becomes the word and each emoji on its own: an emoji is always drawn
    as its own unit, never inside a word."""
    out, cur = [], ""
    for ch in text:
        if ord(ch) in emoji:
            if cur:
                out.append(cur)
            out.append(ch)
            cur = ""
        else:
            cur += ch
    return [*out, cur] if cur else out


def clean_text(text: str, params: dict, emoji: frozenset[int] = frozenset(), *, label: bool = False) -> str:
    """Caption text of one word: quotes and dashes normalised, case applied, trailing commas and full stops
    removed, and every character the caption font cannot draw taken out unless it is an emoji the system
    emoji font has, so the renderer never reaches for another font. Empty when nothing sayable is left."""
    s = "".join(_QUOTES.get(ch, ch) for ch in text.strip())
    s = "".join(ch for ch in s if ord(ch) not in _DROP and ch.isprintable() and not ch.isspace())
    case = params.get("case", "upper")
    if case == "upper":
        s = s.upper()
    elif case == "lower":
        s = s.lower()
    elif case == "title":
        s = s[:1].upper() + s[1:].lower()
    if not label:
        s = s.rstrip(".,;:") or s
    p = font_path(params["font"])
    if p.exists():
        have = font_chars(str(p))
        s = "".join(ch for ch in s if ord(ch) in have or ord(ch) in emoji)
    else:
        s = "".join(ch for ch in s if ord(ch) < 0x250 or ord(ch) in emoji)
    if label:
        return s
    sayable = any(ch.isalnum() or ord(ch) in emoji for ch in s)
    return s if sayable else ""


# ------------------------------------------------------------------ fitting (mirror of core/layout.ts)


def _splits(n: int, k: int):
    if k == 1:
        yield [n]
        return

    def walk(start: int, left: int, cuts: list[int]):
        if left == 1:
            yield [*cuts, n]
            return
        for end in range(start + 1, n - (left - 1) + 1):
            yield from walk(end, left - 1, [*cuts, end])

    yield from walk(0, k, [])


def fit_size(texts: list[tuple[str, bool]], params: dict, box_w: float, box_h: float) -> float:
    """Font size the renderer will set these words at (same algorithm as fitWords in core/layout.ts, with
    Pillow's widths: an estimate good to a few percent, used to decide what goes on a page)."""
    size = float(params["size"])
    ls, gap, lh = params["letterSpacing"], params["wordGap"], params["lineHeight"]
    es = float(params.get("emphScale", 1.0))
    cap, asc, desc = ink_metrics(params["font"])
    upper = params.get("case") == "upper"
    scales = [es if e else 1.0 for _, e in texts]
    grow = float(params.get("grow", 0.0))
    fonts = [params.get("emphFont") or params["font"] if e else params["font"] for _, e in texts]
    w100 = [
        text_width(t, f, 100 * sc, ls) * (1 + 2 * grow)
        for (t, _), sc, f in zip(texts, scales, fonts, strict=True)
    ]
    mx = max([1.0, *scales])
    above = (cap if upper else max(cap, asc)) * mx
    below = (0.03 if upper else desc) * mx
    best = 0.0
    for k in range(1, min(int(params["paging"]["maxLines"]), len(texts)) + 1):
        widest = None
        for ends in _splits(len(texts), k):
            a, m = 0, 0.0
            for b in ends:
                m = max(m, sum(w100[a:b]) + gap * 100 * (b - a - 1))
                a = b
            widest = m if widest is None else min(widest, m)
        h_em = above + below + (k - 1) * lh * mx
        s = min(size, box_w * 100 / max(widest or 1.0, 1e-6), box_h / h_em)
        if s > best * 1.04:
            best = s
    return best


# ------------------------------------------------------------------ the plan


@dataclass
class PWord:
    text: str
    start: float
    end: float
    active_end: float
    emph: bool
    src: int  # index of the transcript word it came from
    moved: bool = False  # start pushed later because it collided with the word before
    stop: int = 0  # punctuation after the word: 2 = sentence end (. ! ?), 1 = comma or the like


@dataclass
class PPage:
    id: int
    start: float
    end: float
    exit: float
    words: list[PWord]


@dataclass
class PLabel:
    id: int
    start: float
    end: float
    exit: float
    text: str
    kind: str


@dataclass
class Plan:
    pages: list[PPage] = field(default_factory=list)
    labels: list[PLabel] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)  # transcript words that are not shown, by index

    @property
    def empty(self) -> bool:
        return not self.pages and not self.labels


def _whole_exit(seconds: float) -> float:
    """An exit animation gets its time or does not happen: squeezed into a frame or two it reads as a
    glitch, so with less than MIN_EXIT_S left the page is simply replaced."""
    return seconds if seconds >= MIN_EXIT_S else 0.0


def plan_labels(
    beats, actions, dur_s: float, params: dict, emoji: frozenset[int] = frozenset()
) -> list[PLabel]:
    """Beat labels (t, kind, label, ...) and action labels (start, end, text): beats win where they
    overlap, a label ends where the next one starts, and anything left too short to read is dropped."""
    raw: list[tuple[float, float, str, str, int]] = []
    for b in beats or []:
        if len(b) > 2 and b[2] and 0 <= b[0] < dur_s:
            kind = b[1] if b[1] in KINDS else "funny"
            raw.append((float(b[0]), min(float(b[0]) + LABEL_S, dur_s), str(b[2]), kind, 0))
    beat_spans = [(s, e) for s, e, *_ in raw]
    for a in actions or []:
        if len(a) > 2 and a[2] and a[1] > a[0] and a[0] < dur_s:
            s, e = max(0.0, float(a[0])), min(float(a[1]), dur_s)
            if not any(bs < e and s < be for bs, be in beat_spans):
                raw.append((s, e, str(a[2]), "action", 1))
    raw.sort(key=lambda r: (r[0], r[4]))
    out: list[PLabel] = []
    exit_s = float(params["label"]["exitS"])
    for i, (s, e, text, kind, _) in enumerate(raw):
        if i + 1 < len(raw):
            e = min(e, raw[i + 1][0])
        text = " ".join(t for t in (clean_text(p, params, emoji, label=True) for p in text.split()) if t)
        if e - s < MIN_LABEL_S or not text:
            continue
        out.append(
            PLabel(
                len(out),
                round(s, 4),
                round(e, 4),
                round(_whole_exit(min(exit_s, (e - s) * 0.4)), 4),
                text[:40],
                kind,
            )
        )
    return out


def plan(
    words: list[Word], dur_s: float, params: dict, *, box_w: float, box_h: float, beats=None, actions=None,
    emoji: frozenset[int] = frozenset(),
) -> Plan:  # fmt: skip
    """Pages and labels for a clip. Times are seconds on the output timeline (the same one the words use)."""
    pg = params["paging"]
    lead = float(pg["leadS"])
    out = Plan(labels=plan_labels(beats, actions, dur_s, params, emoji))
    busy = [(lb.start, lb.end) for lb in out.labels]

    kept: list[PWord] = []
    for i, w in sorted(enumerate(words), key=lambda iw: (iw[1].start, iw[0])):
        text = clean_text(w.text, params, emoji)
        start = max(0.0, float(w.start) - lead)
        if not text or start >= dur_s - 0.02:
            out.dropped.append(i)
            continue
        end = min(max(float(w.end) - lead, start + 0.04), dur_s)
        parts = split_emoji(text, emoji)
        step = (end - start) / len(parts)  # an emoji takes its turn after the word it was attached to
        raw = w.text.strip().rstrip("\"')]")
        stop = 2 if raw.endswith((".", "!", "?", "\u2026")) else 1 if raw.endswith((",", ";", ":")) else 0
        for k, part in enumerate(parts):
            pw = PWord(part, start + k * step, start + (k + 1) * step, end, _emphasis(w), i)
            pw.stop = stop if k == len(parts) - 1 else 0
            kept.append(pw)
    for a, b in zip(kept, kept[1:], strict=False):  # two words cannot be active at once
        if b.start < a.start + MIN_ACTIVE_S:
            b.start, b.moved = a.start + MIN_ACTIVE_S, True
            b.end = max(b.end, b.start + 0.04)
    kept = [w for w in kept if w.start < dur_s - 0.02 or out.dropped.append(w.src)]

    gap_s, linger = float(pg["gapS"]), float(pg["lingerS"])
    for i, w in enumerate(kept):  # the highlight stays until the next word, or lingers into a pause
        nxt = kept[i + 1].start if i + 1 < len(kept) else None
        if nxt is not None and nxt - w.end <= gap_s:
            w.active_end = nxt
        else:
            w.active_end = min(w.end + linger, nxt if nxt is not None else dur_s, dur_s)
        w.active_end = max(w.active_end, min(w.start + MIN_ACTIVE_S, dur_s))
    shown: list[PWord] = []
    for w in kept:  # a label owns the caption spot while it is up
        cut = min((a for a, _ in busy if a > w.start), default=dur_s)  # the next label ends this word's turn
        if any(a <= w.start < b for a, b in busy) or cut - w.start < MIN_SHOWN_S:
            out.dropped.append(w.src)  # spoken under a label, or with no time left before one
            continue
        w.end, w.active_end = min(w.end, cut), min(w.active_end, cut)
        shown.append(w)

    def label_between(t0: float, t1: float) -> bool:
        return any(t0 <= a < t1 for a, _ in busy)

    groups: list[list[PWord]] = []
    floor = float(params["minSize"])
    solo = fit_size([("X", False)], params, box_w, box_h)  # the size short text gets in this box
    for w in shown:
        cur = groups[-1] if groups else None
        # a phrase ends with its sentence, and at a comma once it has two words: pages read as phrases
        ended = cur and (cur[-1].stop == 2 or (cur[-1].stop == 1 and len(cur) >= 2))
        if cur and not ended and len(cur) < int(pg["maxWords"]) and w.start - cur[-1].end <= gap_s:
            if not label_between(cur[-1].start, w.start):
                trial = [(x.text, x.emph) for x in cur] + [(w.text, w.emph)]
                if fit_size(trial, params, box_w, box_h) >= max(min(floor, solo), solo * SHRINK_OK):
                    cur.append(w)
                    continue
        groups.append([w])

    hold, exit_s = float(pg["holdS"]), float(params["timing"]["exitS"])
    overlap, min_show = float(pg["overlapS"]), float(pg["minShowS"])
    for gi, g in enumerate(groups):
        start, last = g[0].start, g[-1]
        nxt = groups[gi + 1][0].start if gi + 1 < len(groups) else None
        wall = min([dur_s, *(a for a, _ in busy if a >= last.start)])  # the clip end or the next label
        if nxt is not None and nxt <= last.active_end + 1e-6 and nxt <= wall:
            end = min(nxt + overlap, wall, groups[gi + 1][-1].active_end) if overlap > 0 else nxt
            ex = max(0.0, end - nxt)
        else:
            limit = min(wall, nxt if nxt is not None else dur_s)
            end = min(max(last.active_end + hold, start + min_show), limit)
            ex = _whole_exit(min(exit_s, end - last.active_end, (end - start) * 0.5))
        if end - start < 1e-3:
            out.dropped.extend(w.src for w in g)
            continue
        for w in g:
            w.active_end = min(w.active_end, end)
        out.pages.append(PPage(len(out.pages), round(start, 4), round(end, 4), round(ex, 4), g))
    for p in out.pages:
        for w in p.words:
            w.start, w.end, w.active_end = round(w.start, 4), round(w.end, 4), round(w.active_end, 4)
    out.dropped.sort()
    return out
