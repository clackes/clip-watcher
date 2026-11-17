# ruff: noqa: E501
"""ASS caption writer (D5).

Styles:
- classic: 1-3 words per page, current word #FFE600 at 110%.
- bounce: 1-2 words (max ~14 chars) per page, page pops in 70% -> 118% -> 100%, current word in a
  colour that rotates per page.
- pop (default): the "scale-up" look of MrBeast-style clippers. Anton, thick black stroke, up to 3 words
  per page on one line, white; the spoken word turns yellow and pops 100% -> 135% -> 125%. Every word is its own positioned event so a growing word scales around its own
  centre instead of pushing the line around.
"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path

from clipwatcher import config
from clipwatcher.models import Word
from clipwatcher.render.branding import FONTS_DIR

HIGHLIGHT = "&H00E6FF&"  # #FFE600 in ASS BGR order
CAPTION_STYLES = ("classic", "bounce", "pop")
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
DEMO_TEXT = "NO WAY DID YOU SEE THAT HE JUST WENT ALL IN AND IT ACTUALLY WORKED CHAT GO CRAZY"


def caption_style(clip_id: str) -> str:
    """Caption style from render.captions: classic | bounce | pop | ab (stable classic/pop split by clip_id)."""
    mode = str(config.global_config().get("render", {}).get("captions", "pop"))
    if mode == "ab":
        return ("classic", "pop")[int(hashlib.sha256(f"cap:{clip_id}".encode()).hexdigest(), 16) % 2]
    return mode if mode in CAPTION_STYLES else "pop"


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


def build_ass(words: list[Word], dur_s: float, credit: str | None = None, style: str = "classic") -> str:
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
Style: Credit,Montserrat ExtraBold,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,4,0,9,100,20,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if credit:
        lines.append(f"Dialogue: 1,{ts(0)},{ts(dur_s)},Credit,,0,0,0,,{{\\an9\\pos(1060,282)}}{esc(credit)}")
    if style == "pop":
        return head + "\n".join(lines + build_pop_events(words, dur_s)) + "\n"
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
            pos = "\\an5\\pos(540,1495)" + (BOUNCE_POP if bounce and i == 0 else "")
            name = "Pop" if bounce else "Cap"
            lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},{name},,0,0,0,,{{{pos}}}{' '.join(parts)}")
    return head + "\n".join(lines) + "\n"


def write_ass(
    path: Path, words: list[Word], dur_s: float, credit: str | None = None, style: str = "classic"
) -> Path:
    path.write_text(build_ass(words, dur_s, credit, style), encoding="utf-8")
    return path
