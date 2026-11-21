"""Placeholder branding assets and the per-clip hook pill (Pillow only)."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from clipwatcher.config import ROOT

BRANDING_DIR = ROOT / "assets" / "branding"
FONTS_DIR = ROOT / "assets" / "fonts"

ACCENTS: dict[str, tuple[int, int, int]] = {
    "red": (255, 45, 60),
    "yellow": (255, 230, 0),
    "cyan": (0, 229, 255),
}

_FONT_CANDIDATES = [
    FONTS_DIR / "Montserrat-ExtraBold.ttf",
    Path("/System/Library/Fonts/Supplemental/Arial Black.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("C:/Windows/Fonts/ariblk.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("DejaVuSans-Bold.ttf"),
]


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in _FONT_CANDIDATES:
        try:
            if p.exists() or not p.is_absolute():
                return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _text_w(font, text: str) -> int:
    x0, _, x1, _ = font.getbbox(text)
    return int(x1 - x0)


def _draw_centered(draw: ImageDraw.ImageDraw, box: tuple[int, int], text: str, font, fill) -> None:
    w, h = box
    draw.text((w / 2, h / 2), text, font=font, fill=fill, anchor="mm")


def _strip_emoji(text: str) -> str:
    import unicodedata

    return "".join(
        c
        for c in text
        if unicodedata.category(c) != "So" and c not in "\u200d\ufe0f\ufe0e" and not 0x1F000 <= ord(c)
    )


def hook_pill(text: str, out: Path | None = None) -> Image.Image:
    """Rounded black pill, 96 px high, at most 1000 px wide. Saved to ``out`` when given.

    Emoji are dropped: the pill font has no emoji glyphs and drew an empty box (they stay in the title).
    """
    text = " ".join(_strip_emoji(text).split()).upper()
    size = 60
    font = load_font(size)
    while size > 20 and _text_w(font, text) > 1000 - 64:
        size -= 2
        font = load_font(size)
    w = min(1000, max(240, _text_w(font, text) + 64))
    img = Image.new("RGBA", (w, 96), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, 95), radius=48, fill=(0, 0, 0, 235))
    _draw_centered(d, (w, 96), text, font, (255, 255, 255, 255))
    if out is not None:
        img.save(out)
    return img


def _cta(text: str, accent: tuple[int, int, int]) -> Image.Image:
    w, h = 840, 120
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=60, fill=(16, 16, 22, 235), outline=accent, width=5)
    font = load_font(44)
    _draw_centered(d, (w, h), text, font, (255, 255, 255, 255))
    return img


def _logo() -> Image.Image:
    w, h = 800, 110
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=24, fill=(16, 16, 22, 200), outline=ACCENTS["red"], width=4
    )
    _draw_centered(d, (w, h), "CLIP WATCHER", load_font(56), (255, 255, 255, 255))
    return img


def _gradient(accent: tuple[int, int, int]) -> Image.Image:
    w, h = 1080, 1920
    top, bot = (12, 12, 20), (34, 20, 44)
    col = Image.linear_gradient("L").resize((w, h))
    img = Image.composite(Image.new("RGB", (w, h), bot), Image.new("RGB", (w, h), top), col)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 262, w, 269), fill=accent)
    d.rectangle((0, 1290, w, 1297), fill=accent)
    return img


def generate_all(
    out_dir: Path | None = None, *, hook: str | None = None, overwrite: bool = False
) -> list[Path]:
    """Write placeholder branding PNGs; existing files are kept unless ``overwrite``."""
    out = Path(out_dir) if out_dir else BRANDING_DIR
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    def save(name: str, make) -> None:
        p = out / name
        paths.append(p)
        if overwrite or not p.exists():
            make().save(p)

    save("top_logo.png", _logo)
    save("cta_youtube.png", lambda: _cta("SUBSCRIBE · LIKE · COMMENT", ACCENTS["red"]))
    save("cta_social.png", lambda: _cta("FOLLOW · LIKE · COMMENT", ACCENTS["cyan"]))
    save("bottom_cta.png", lambda: _cta("SUBSCRIBE · LIKE · COMMENT", ACCENTS["red"]))
    for name, rgb in ACCENTS.items():
        save(f"frame_gradient_{name}.png", lambda rgb=rgb: _gradient(rgb))
    if hook:
        p = out / "hook_pill.png"
        hook_pill(hook, p)
        paths.append(p)
    return paths


def ensure_branding(out_dir: Path | None = None) -> Path:
    out = Path(out_dir) if out_dir else BRANDING_DIR
    generate_all(out)
    return out


# -- the top banner the companion app lets the owner set ------------------------------------------------------
# The split layout scales it to a fixed width (render/template.py SPLIT_LOGO_W), so a wide banner (about 7:1,
# like the placeholder's 800x110) sits best. Uploads are kept here; the render reads this file at each render.
LOGO_FILE = BRANDING_DIR / "top_logo.png"
_LOGO_MARK = BRANDING_DIR / ".top_logo.custom"  # present once the owner uploaded their own banner
_LOGO_MAX = (1200, 400)  # the stored banner is fit inside this; the render scales it to the layout


def logo_file() -> Path:
    """The banner overlaid on clips; the placeholder is written first if nothing is there yet."""
    if not LOGO_FILE.exists():
        ensure_branding(BRANDING_DIR)
    return LOGO_FILE


def logo_is_custom() -> bool:
    """True when the current banner is one the owner uploaded (not the generated placeholder)."""
    return _LOGO_MARK.exists() and LOGO_FILE.exists()


def save_logo(raw: bytes) -> dict[str, object]:
    """Keep uploaded image bytes as the clip banner (assets/branding/top_logo.png). ValueError on anything
    Pillow cannot open as an image. The file is written whole (temp then rename) so a render never reads half."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:  # noqa: BLE001 - any unreadable upload is a bad request, not a crash
        raise ValueError("That file is not an image the app can read (PNG, JPG or WebP).") from e
    img = img.convert("RGBA")
    img.thumbnail(_LOGO_MAX, Image.LANCZOS)
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LOGO_FILE.with_name(LOGO_FILE.name + ".tmp")
    img.save(tmp, "PNG")
    tmp.replace(LOGO_FILE)
    _LOGO_MARK.write_text("1", encoding="utf-8")
    return {"ok": True, "custom": True, "width": img.width, "height": img.height}


def reset_logo() -> dict[str, object]:
    """Put the generated placeholder banner back."""
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    _logo().save(LOGO_FILE)
    _LOGO_MARK.unlink(missing_ok=True)
    return {"ok": True, "custom": False}
