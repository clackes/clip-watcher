# ruff: noqa: E501
"""ASS caption writer: 1-3 words per event, current word highlighted (D5)."""

from __future__ import annotations

from pathlib import Path

from clipwatcher.models import Word
from clipwatcher.render.branding import FONTS_DIR

HIGHLIGHT = "&H00E6FF&"  # #FFE600 in ASS BGR order
DEMO_TEXT = "NO WAY DID YOU SEE THAT HE JUST WENT ALL IN AND IT ACTUALLY WORKED CHAT GO CRAZY"


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


def group_words(words: list[Word], max_words: int = 3, max_gap: float = 0.6) -> list[list[Word]]:
    groups: list[list[Word]] = []
    for w in words:
        if not w.text.strip():
            continue
        if groups and len(groups[-1]) < max_words and w.start - groups[-1][-1].end <= max_gap:
            groups[-1].append(w)
        else:
            groups.append([w])
    return groups


def build_ass(words: list[Word], dur_s: float, credit: str | None = None) -> str:
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
Style: Credit,Montserrat ExtraBold,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,4,0,9,100,20,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if credit:
        lines.append(f"Dialogue: 1,{ts(0)},{ts(dur_s)},Credit,,0,0,0,,{{\\an9\\pos(1060,282)}}{esc(credit)}")
    for group in group_words(words):
        for i, cur in enumerate(group):
            start = cur.start
            end = group[i + 1].start if i + 1 < len(group) else max(cur.end, cur.start + 0.15)
            end = min(end, dur_s)
            if end <= start:
                continue
            parts = []
            for j, w in enumerate(group):
                t = esc(w.text.strip().upper())
                if j == i:
                    parts.append(f"{{\\c{HIGHLIGHT}\\fscx110\\fscy110}}{t}{{\\r}}")
                else:
                    parts.append(t)
            lines.append(
                f"Dialogue: 0,{ts(start)},{ts(end)},Cap,,0,0,0,,{{\\an5\\pos(540,1495)}}{' '.join(parts)}"
            )
    return head + "\n".join(lines) + "\n"


def write_ass(path: Path, words: list[Word], dur_s: float, credit: str | None = None) -> Path:
    path.write_text(build_ass(words, dur_s, credit), encoding="utf-8")
    return path
