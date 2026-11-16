"""Placeholder branding assets and the per-clip hook pill (Pillow only)."""

from __future__ import annotations

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


def hook_pill(text: str, out: Path | None = None) -> Image.Image:
    """Rounded black pill, 96 px high, at most 1000 px wide. Saved to ``out`` when given."""
    text = text.strip().upper()
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
