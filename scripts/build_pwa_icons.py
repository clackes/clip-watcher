"""Build the companion app's PWA icons from ClipVision's brand (Pillow only).

One gradient square (blue -> coral -> purple) with a white play/clip mark, rendered at the sizes a
progressive web app and the home-screen need. Run after a brand change:

    python scripts/build_pwa_icons.py

Writes into src/clipwatcher/ui/static/img/.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "src" / "clipwatcher" / "ui" / "static" / "img"

# ClipVision's gradient, 135deg: blue -> coral -> purple (app.css --grad).
STOPS = [(0.0, (96, 159, 238)), (0.6604, (255, 157, 154)), (1.0, (171, 75, 218))]
GROUND = (17, 17, 28)  # --bg


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _grad_color(t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    for (p0, c0), (p1, c1) in zip(STOPS, STOPS[1:], strict=False):
        if t <= p1:
            return _lerp(c0, c1, (t - p0) / (p1 - p0) if p1 > p0 else 0.0)
    return STOPS[-1][1]


def _gradient(size: int, ss: int = 4) -> Image.Image:
    """A 135deg diagonal gradient, supersampled for smoothness."""
    n = size * ss
    img = Image.new("RGB", (n, n))
    px = img.load()
    for y in range(n):
        for x in range(n):
            # 135deg: along the top-left -> bottom-right diagonal
            t = (x + y) / (2 * (n - 1))
            px[x, y] = _grad_color(t)
    return img.resize((size, size), Image.LANCZOS)


def _play(size: int, inset: float) -> Image.Image:
    """A white, soft-cornered play triangle centred in the square (inset is the margin fraction)."""
    ss = 4
    n = size * ss
    layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    m = n * inset
    w = n - 2 * m
    # optical centring: nudge the triangle right a touch
    left = m + w * 0.08
    top = m + w * 0.04
    bot = n - m - w * 0.04
    right = left + (bot - top) * 0.92
    mid = (top + bot) / 2
    d.polygon([(left, top), (right, mid), (left, bot)], fill=(255, 255, 255, 255))
    layer = layer.resize((size, size), Image.LANCZOS)
    return layer


def _rounded_mask(size: int, radius_frac: float) -> Image.Image:
    ss = 4
    n = size * ss
    m = Image.new("L", (n, n), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, n - 1, n - 1), radius=int(n * radius_frac), fill=255)
    return m.resize((size, size), Image.LANCZOS)


def icon(size: int, *, maskable: bool = False, rounded: bool = True) -> Image.Image:
    base = Image.new("RGBA", (size, size), (*GROUND, 255))
    grad = _gradient(size).convert("RGBA")
    # maskable icons keep a safe zone: the glyph sits smaller, the gradient bleeds to the edge
    inset = 0.30 if maskable else 0.28
    grad.alpha_composite(_play(size, inset))
    if maskable or not rounded:
        base.alpha_composite(grad)
    else:
        base.paste(grad, (0, 0), _rounded_mask(size, 0.22))
    return base


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = {
        "icon-192.png": icon(192),
        "icon-512.png": icon(512),
        "icon-maskable-512.png": icon(512, maskable=True),
        "apple-touch-icon.png": icon(180, rounded=False),  # iOS rounds it itself
        "favicon-32.png": icon(32),
    }
    for name, im in jobs.items():
        im.save(OUT / name)
        print("wrote", (OUT / name).name, im.size)


if __name__ == "__main__":
    main()
