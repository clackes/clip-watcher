# ruff: noqa: E501
"""ASS caption writer (D5).

Styles:
- classic: 1-3 words per page, current word #FFE600 at 110%.
- bounce: 1-2 words (max ~14 chars) per page, page pops in 70% -> 118% -> 100%, current word in a
  colour that rotates per page. Timings follow the reaction-clip presets popular clippers use.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from clipwatcher import config
from clipwatcher.models import Word
from clipwatcher.render.branding import FONTS_DIR

HIGHLIGHT = "&H00E6FF&"  # #FFE600 in ASS BGR order
CAPTION_STYLES = ("classic", "bounce")
# #FFE600 yellow, #00F0FF cyan, #7CFF3A lime, #FF8A1E orange (BGR)
BOUNCE_COLOURS = ("&H00E6FF&", "&HFFF000&", "&H3AFF7C&", "&H1E8AFF&")
BOUNCE_POP = "\\fscx70\\fscy70\\t(0,90,\\fscx118\\fscy118)\\t(90,180,\\fscx100\\fscy100)"
DEMO_TEXT = "NO WAY DID YOU SEE THAT HE JUST WENT ALL IN AND IT ACTUALLY WORKED CHAT GO CRAZY"


def caption_style(clip_id: str) -> str:
    """Caption style from render.captions: classic | bounce | ab (stable 50/50 split by clip_id)."""
    mode = str(config.global_config().get("render", {}).get("captions", "classic"))
    if mode == "ab":
        return CAPTION_STYLES[int(hashlib.sha256(f"cap:{clip_id}".encode()).hexdigest(), 16) % 2]
    return mode if mode in CAPTION_STYLES else "classic"


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
Style: Credit,Montserrat ExtraBold,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,4,0,9,100,20,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if credit:
        lines.append(f"Dialogue: 1,{ts(0)},{ts(dur_s)},Credit,,0,0,0,,{{\\an9\\pos(1060,282)}}{esc(credit)}")
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
