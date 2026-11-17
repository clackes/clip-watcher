"""Scale the ClipVault banners in assets/branding/src/ to the 9:16 template slots (rounded corners).

python scripts/build_brand_assets.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

B = Path(__file__).resolve().parents[1] / "assets" / "branding"
TOP_W = 1000  # top banner: 1000 wide, centred, ends above the webcam at y270
CTA_W = 920  # CTA banner under the gameplay


def rounded(src: Path, width: int, radius: int = 50) -> Image.Image:
    im = Image.open(src).convert("RGBA")
    h = round(im.height * width / im.width)
    im = im.resize((width, h), Image.LANCZOS)
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, h - 1), radius=radius, fill=255)
    im.putalpha(mask)
    return im


def main() -> None:
    top = rounded(B / "src" / "top_banner.png", TOP_W)
    top.save(B / "top_logo.png")
    cta = rounded(B / "src" / "cta_banner.jpg", CTA_W)
    for name in ("cta_youtube.png", "cta_social.png", "bottom_cta.png"):
        cta.save(B / name)
    print("top", top.size, "cta", cta.size)


if __name__ == "__main__":
    main()
