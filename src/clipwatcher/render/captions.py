# ruff: noqa: E501
"""ASS caption writer (D5).

Styles:
- classic: 1-3 words per page, current word #FFE600 at 110%.
- bounce: 1-2 words (max ~14 chars) per page, page pops in 70% -> 118% -> 100%, current word in a
  colour that rotates per page.
- pop (default): the "scale-up" look of MrBeast-style clippers. Anton, thick black stroke, up to 3 words
  per page on one line, white; the spoken word turns yellow and pops 100% -> 135% -> 125%. Every word is its own positioned event so a growing word scales around its own
  centre instead of pushing the line around.
- word (default since 17 Nov evening): the split-screen clipper look. One word at a time on the seam between
  webcam and gameplay, Anton, white with a black stroke; each word pops in 80% -> 110% -> 100%. Shouted
  words (exclamations, "no", "what", "bro", numbers...) turn yellow. Judge beats show as coloured
  sound-effect labels (*FAIL* red, *SUCCESS* green) in place of the words.
"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path

from clipwatcher import config
from clipwatcher.models import Word
from clipwatcher.render.branding import FONTS_DIR

HIGHLIGHT = "&H00E6FF&"  # #FFE600 in ASS BGR order
CREDIT_Y = 407  # streamer credit, top-right of the panel (template.PANEL_Y + 12)
CAPTION_STYLES = ("classic", "bounce", "pop", "word")
POP_FONT = ("Anton", "Anton-Regular.ttf")
POP_SIZE = 145  # ASS size: libass fits winAscent+winDescent (1.733 em for Anton) in it -> 72 px caps
POP_EM = POP_SIZE / 1.733  # glyph em in PlayRes px, used to measure widths with PIL
POP_MAX_W = 960  # px of a page line, with room left for the active word's growth
POP_GROW = 0.13  # each side of a word gets this share of its width as room to grow (125% hold)
# Highlight colours, cycled per word (#FFE600 yellow, BGR). Owner, 17 Nov: one colour, the rest white.
POP_COLOURS = ("&H00E6FF&",)
POP_ACTIVE = "\\fscx100\\fscy100\\t(0,70,\\fscx135\\fscy135)\\t(70,150,\\fscx125\\fscy125)"
# #FFE600 yellow, #00F0FF cyan, #7CFF3A lime, #FF8A1E orange (BGR)
BOUNCE_COLOURS = ("&H00E6FF&", "&HFFF000&", "&H3AFF7C&", "&H1E8AFF&")
BOUNCE_POP = "\\fscx70\\fscy70\\t(0,90,\\fscx118\\fscy118)\\t(90,180,\\fscx100\\fscy100)"
WORD_SIZE = 130  # ~65 px caps, as the reference split Shorts (owner, 19 Nov)
WORD_POP = "\\fscx80\\fscy80\\t(0,60,\\fscx110\\fscy110)\\t(60,130,\\fscx100\\fscy100)"
LABEL_POP = "\\fscx60\\fscy60\\t(0,80,\\fscx120\\fscy120)\\t(80,180,\\fscx105\\fscy105)"
LABEL_S = 0.9
# label colours per beat kind (BGR): fail red, hype green, shock white, funny yellow
LABEL_COLOURS = {"fail": "&H2A2AFF&", "hype": "&H3CFF3C&", "shock": "&HFFFFFF&", "funny": "&H00E6FF&"}
EMPHASIS = frozenset(
    "no nah what wait bro yes yeah oh omg god why how never insane crazy stop run go dude bruh huh hello "
    "help please lets let's what's yo wow".split()
)
DEMO_TEXT = "NO WAY DID YOU SEE THAT HE JUST WENT ALL IN AND IT ACTUALLY WORKED CHAT GO CRAZY"


def caption_style(clip_id: str) -> str:
    """Caption style from render.captions: classic | bounce | pop | ab (stable classic/pop split by clip_id)
    | rm:<template> (an animated Remotion template, render/remotion.py; render_short falls back to an ASS
    style when it cannot be rendered)."""
    mode = str(config.global_config().get("render", {}).get("captions", "word"))
    if mode.startswith("rm:") and len(mode) > 3:
        return mode
    if mode == "ab":
        return ("classic", "pop")[int(hashlib.sha256(f"cap:{clip_id}".encode()).hexdigest(), 16) % 2]
    return mode if mode in CAPTION_STYLES else "word"


@functools.lru_cache(maxsize=1)
def _pop_font():
    from PIL import ImageFont

    p = FONTS_DIR / POP_FONT[1]
    return ImageFont.truetype(str(p), round(POP_EM)) if p.exists() else None


def text_width(text: str) -> float:
    """Rendered width of text in the pop style, in PlayRes pixels (estimate when the font is missing)."""
    f = _pop_font()
    return float(f.getlength(text)) if f else len(text) * POP_EM * 0.42


def pop_text(w: Word) -> str:
    """Upper-case word without trailing commas/full stops (kept: ? ! and apostrophes)."""
    return w.text.strip().upper().rstrip(".,;:") or w.text.strip().upper()


def pop_pages(words: list[Word], max_words: int = 3, max_gap: float = 0.6) -> list[list[Word]]:
    """Pages that fit one line at POP_MAX_W, counting the spaces between words."""
    space = text_width(" ")
    pages: list[list[Word]] = []
    for w in words:
        t = pop_text(w)
        if not t:
            continue
        if pages:
            cur = pages[-1]
            ws = [text_width(pop_text(x)) for x in cur] + [text_width(t)]
            width = sum(ws) * (1 + 2 * POP_GROW) + space * len(cur)
            if len(cur) < max_words and w.start - cur[-1].end <= max_gap and width <= POP_MAX_W:
                cur.append(w)
                continue
        pages.append([w])
    return pages


def build_pop_events(words: list[Word], dur_s: float, y: int = 1495) -> list[str]:
    pages = pop_pages(words)
    space = text_width(" ")
    lines, k = [], 0
    for p, page in enumerate(pages):
        texts = [esc(pop_text(w)) for w in page]
        widths = [text_width(t) for t in texts]
        gaps = [space + POP_GROW * (widths[i] + widths[i + 1]) for i in range(len(page) - 1)]
        x = 540 - (sum(widths) + sum(gaps)) / 2
        nxt = pages[p + 1][0].start if p + 1 < len(pages) else dur_s
        page_end = min(max(page[-1].end + 0.25, page[-1].start + 0.3), nxt, dur_s)
        p0 = page[0].start
        for i, (w, t, wd) in enumerate(zip(page, texts, widths, strict=True)):
            colour = POP_COLOURS[k % len(POP_COLOURS)]
            k += 1
            cx = round(x + wd / 2)
            x += wd + (gaps[i] if i < len(gaps) else 0)
            a0 = max(w.start, p0)
            a1 = page[i + 1].start if i + 1 < len(page) else page_end
            pos = f"\\an5\\pos({cx},{y})"
            spans = [
                (p0, a0, 0, f"{{{pos}}}{t}"),  # not said yet: white
                (a0, a1, 2, f"{{{pos}\\c{colour}{POP_ACTIVE}}}{t}"),  # being said: pops, own colour
                (a1, page_end, 1, f"{{{pos}}}{t}"),  # said: back to white
            ]
            for s, e, layer, txt in spans:
                e = min(e, dur_s)
                if e - s >= 0.01:
                    lines.append(f"Dialogue: {layer},{ts(s)},{ts(e)},Pop3,,0,0,0,,{txt}")
    return lines


def _emphasis(w: Word) -> bool:
    raw = w.text.strip()
    t = raw.lower().strip(".,;:?!\"'")
    return raw.endswith("!") or t in EMPHASIS or any(ch.isdigit() for ch in t)


def build_word_events(
    words: list[Word], dur_s: float, y: int, beats: list[tuple] | None = None,
    actions: list[tuple] | None = None,
) -> list[str]:  # fmt: skip
    """One word per event at (540, y); labelled beats replace the words for LABEL_S seconds."""
    labels = [(b[0], b[1], b[2]) for b in beats or [] if b[2]]
    busy = [(t, min(t + LABEL_S, dur_s)) for t, _, _ in labels]
    acts = [(a[0], min(a[1], dur_s), str(a[2])) for a in actions or [] if a[2] and a[1] > a[0]]
    busy += [(a, b) for a, b, _ in acts]
    ws = [w for w in words if any(ch.isalnum() for ch in pop_text(w))]  # no bare '!?' or '[...]'
    lines = []
    for i, w in enumerate(ws):
        s0 = w.start
        nxt = ws[i + 1].start if i + 1 < len(ws) else dur_s
        e0 = nxt if nxt - w.end <= 0.6 else w.end + 0.2
        e0 = min(max(e0, s0 + 0.12), dur_s)
        if any(a < e0 and s0 < b for a, b in busy):
            continue
        colour = "\\c&H00E6FF&" if _emphasis(w) else ""
        txt = f"{{\\an5\\pos(540,{y}){colour}{WORD_POP}}}{esc(pop_text(w))}"
        if e0 - s0 >= 0.01:
            lines.append(f"Dialogue: 0,{ts(s0)},{ts(e0)},Word,,0,0,0,,{txt}")
    for a, b, tx in acts:  # *WATCHING*, *STARTS CHEWING*: what happens while nobody talks
        if any(t < b and a < t + LABEL_S for t, _, _ in labels):
            continue
        txt = f"{{\\an5\\pos(540,{y}){LABEL_POP}}}{esc(tx)}"
        lines.append(f"Dialogue: 1,{ts(a)},{ts(b)},Word,,0,0,0,,{txt}")
    for t, k, lb in labels:
        c = LABEL_COLOURS.get(k, "&HFFFFFF&")
        txt = f"{{\\an5\\pos(540,{y})\\c{c}{LABEL_POP}}}{esc(lb)}"
        lines.append(f"Dialogue: 2,{ts(t)},{ts(min(t + LABEL_S, dur_s))},Word,,0,0,0,,{txt}")
    return lines


def esc(text: str) -> str:
    """Escape ASS override-block delimiters and backslashes."""
    return text.replace("\\", "／").replace("{", "(").replace("}", ")").replace("\n", " ")


def ts(sec: float) -> str:
    cs = round(max(sec, 0.0) * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def demo_words(dur_s: float, text: str = DEMO_TEXT) -> list[Word]:
    toks = text.split()
    step = max(dur_s - 0.5, 0.5) / len(toks)
    return [Word(start=0.3 + i * step, end=0.3 + (i + 1) * step - 0.02, text=t) for i, t in enumerate(toks)]


def group_words(
    words: list[Word], max_words: int = 3, max_gap: float = 0.6, max_chars: int | None = None
) -> list[list[Word]]:
    groups: list[list[Word]] = []
    for w in words:
        if not w.text.strip():
            continue
        fits = max_chars is None or (
            groups and sum(len(x.text.strip()) + 1 for x in groups[-1]) + len(w.text.strip()) <= max_chars
        )
        if groups and fits and len(groups[-1]) < max_words and w.start - groups[-1][-1].end <= max_gap:
            groups[-1].append(w)
        else:
            groups.append([w])
    return groups


def build_ass(
    words: list[Word], dur_s: float, credit: str | None = None, style: str = "classic", *, y: int = 1495,
    beats: list[tuple] | None = None, credit_pos: tuple[int, int, int] | None = None,
    actions: list[tuple] | None = None,
) -> str:  # fmt: skip
    """credit_pos = (alignment, x, y) of the streamer credit; default top-right of the stack panel."""
    if style not in CAPTION_STYLES:
        raise ValueError(f"bad caption style: {style}")
    bounce = style == "bounce"
    has_font = (FONTS_DIR / "Montserrat-ExtraBold.ttf").exists()
    bold = 0 if has_font else -1
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Montserrat ExtraBold,80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,8,0,5,100,160,0,1
Style: Pop,Montserrat ExtraBold,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,{bold},0,0,0,100,100,0,0,1,10,3,5,100,160,0,1
Style: Pop3,{POP_FONT[0]},{POP_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,1,0,1,8,4,5,60,60,0,1
Style: Word,{POP_FONT[0]},{WORD_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,9,4,5,40,40,0,1
Style: Credit,Montserrat ExtraBold,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,4,0,9,100,20,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if credit:
        an, cx, cy = credit_pos or (9, 1060, CREDIT_Y)
        lines.append(
            f"Dialogue: 1,{ts(0)},{ts(dur_s)},Credit,,0,0,0,,{{\\an{an}\\pos({cx},{cy})}}{esc(credit)}"
        )
    if style == "pop":
        return head + "\n".join(lines + build_pop_events(words, dur_s, y)) + "\n"
    if style == "word":
        return head + "\n".join(lines + build_word_events(words, dur_s, y, beats, actions)) + "\n"
    groups = group_words(words, max_words=2, max_chars=14) if bounce else group_words(words)
    for g, group in enumerate(groups):
        colour = BOUNCE_COLOURS[g % len(BOUNCE_COLOURS)] if bounce else HIGHLIGHT
        for i, cur in enumerate(group):
            start = cur.start
            end = group[i + 1].start if i + 1 < len(group) else max(cur.end, cur.start + 0.15)
            end = min(end, dur_s)
            if end <= start:
                continue
            parts = []
            for j, w in enumerate(group):
                t = esc(w.text.strip().upper())
                if j == i and bounce:
                    parts.append(f"{{\\c{colour}}}{t}{{\\c&HFFFFFF&}}")
                elif j == i:
                    parts.append(f"{{\\c{colour}\\fscx110\\fscy110}}{t}{{\\r}}")
                else:
                    parts.append(t)
            # Bounce pops the page in once, on its first word; the per-word events after it stay still.
            pos = f"\\an5\\pos(540,{y})" + (BOUNCE_POP if bounce and i == 0 else "")
            name = "Pop" if bounce else "Cap"
            lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},{name},,0,0,0,,{{{pos}}}{' '.join(parts)}")
    return head + "\n".join(lines) + "\n"


def write_ass(
    path: Path, words: list[Word], dur_s: float, credit: str | None = None, style: str = "classic", **kw
) -> Path:
    path.write_text(build_ass(words, dur_s, credit, style, **kw), encoding="utf-8")
    return path
