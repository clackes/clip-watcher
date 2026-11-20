#!/usr/bin/env python3
"""Which enlargement suits a small webcam best? Measured against the truth, not by eye alone.

A webcam overlay is a few hundred pixels wide and fills a 1080x960 panel of the Short: 3 to 4 times larger.
No scaler brings back what the overlay never had, but they differ in what they add (blur, ringing, halos).
To measure that, the truth is needed: a face at full size. So this takes head-and-shoulders crops from
full-screen camera recordings (the truth), shrinks them to webcam size (optionally through an H.264 pass,
as a stream's encoder would), enlarges them back with each candidate filter chain and compares with the
truth: PSNR, SSIM, fine detail kept (variance of the Laplacian) and halos (pixels pushed outside the range
of their neighbourhood in the truth).

.venv/bin/python scripts/webcam_sharpness.py ROOT [--factor 3.4] [--crf 26] login:t:x:y:w:h [...]
ROOT/rec/<login>.ts; x y w h = a head box at media time t (the crop is taken around it).
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

CHAINS = {
    "bilinear": "scale={w}:{h}:flags=bilinear",
    "bicubic": "scale={w}:{h}:flags=bicubic",
    "lanczos (as shipped)": "scale={w}:{h}:flags=lanczos",
    "spline": "scale={w}:{h}:flags=spline",
    "lanczos+accurate": "scale={w}:{h}:flags=lanczos+accurate_rnd+full_chroma_int",
    "lanczos, unsharp 0.3": "scale={w}:{h}:flags=lanczos,unsharp=5:5:0.3:5:5:0",
    "lanczos, unsharp 0.6": "scale={w}:{h}:flags=lanczos,unsharp=5:5:0.6:5:5:0",
    "lanczos, unsharp 1.0": "scale={w}:{h}:flags=lanczos,unsharp=5:5:1.0:5:5:0",
    "lanczos, unsharp 7x7 0.6": "scale={w}:{h}:flags=lanczos,unsharp=7:7:0.6:7:7:0",
    "lanczos, cas 0.4": "scale={w}:{h}:flags=lanczos,cas=0.4",
    "2x canvas + zoompan (beats)": (
        "scale={w2}:{h2}:flags=lanczos,zoompan=z=1:x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={w}x{h}:fps=1"
    ),
}


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def halos(out, truth) -> float:
    """Share of pixels more than 8 levels outside the range their 5x5 neighbourhood has in the truth."""
    import cv2
    import numpy as np

    k = np.ones((5, 5), np.uint8)
    hi, lo = cv2.dilate(truth, k).astype(np.int16), cv2.erode(truth, k).astype(np.int16)
    o = out.astype(np.int16)
    return float(((o > hi + 8) | (o < lo - 8)).mean())


def main() -> int:
    import cv2
    import webcam_check as wc

    from clipwatcher import calibrate, config

    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("heads", nargs="+")
    ap.add_argument("--factor", type=float, default=3.4)
    ap.add_argument("--crf", type=int, default=0, help="H.264 pass on the small picture (0 = none)")
    a = ap.parse_args()
    ff = config.ffmpeg_bin()
    table: dict[str, list[tuple[float, float, float, float]]] = {k: [] for k in CHAINS}
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        for i, spec in enumerate(a.heads):
            login, t, x, y, w, h = spec.split(":")
            x, y, w, h = int(x), int(y), int(w), int(h)
            frame = work / f"f{i}.png"
            if not calibrate.grab_frame(a.root / "rec" / f"{login}.ts", float(t), frame):
                continue
            im = cv2.imread(str(frame))
            fh, fw = im.shape[:2]
            ch = min(fh, int(2.6 * h)) // 16 * 16
            cw = min(fw, int(ch * 1.125)) // 16 * 16
            ch = int(cw / 1.125) // 2 * 2
            cx = min(max(0, x + w // 2 - cw // 2), fw - cw)
            cy = min(max(0, y - int(0.75 * h)), fh - ch)
            truth = im[cy : cy + ch, cx : cx + cw]
            lw, lh = int(cw / a.factor) // 2 * 2, int(ch / a.factor) // 2 * 2
            small = cv2.resize(truth, (lw, lh), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(work / "small.png"), small)
            src = work / "small.png"
            if a.crf:  # what the stream's encoder leaves of an overlay this size
                run([ff, "-y", "-loglevel", "error", "-loop", "1", "-i", str(src), "-frames:v", "12", "-c:v",
                     "libx264", "-preset", "veryfast", "-crf", str(a.crf), "-pix_fmt", "yuv420p",
                     str(work / "small.mp4")])  # fmt: skip
                run([ff, "-y", "-loglevel", "error", "-i", str(work / "small.mp4"), "-vf",
                     "select=eq(n\\,11)", "-frames:v", "1", str(work / "small2.png")])  # fmt: skip
                src = work / "small2.png"
            # Luma only, as the renderer has it: yuv420p in, yuv420p out, never through RGB
            cv2.imwrite(str(work / "truth.png"), truth)
            luma = ["-vf", "format=yuv420p", "-pix_fmt", "gray", "-frames:v", "1"]
            run(
                [
                    ff,
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(work / "truth.png"),
                    *luma,
                    str(work / "truth_y.png"),
                ]
            )
            tg = cv2.imread(str(work / "truth_y.png"), cv2.IMREAD_GRAYSCALE)
            for name, chain in CHAINS.items():
                vf = "format=yuv420p," + chain.format(w=cw, h=ch, w2=cw * 2, h2=ch * 2) + ",format=yuv420p"
                out = work / "out.png"
                run([ff, "-y", "-loglevel", "error", "-i", str(src), "-vf", vf, "-pix_fmt", "gray",
                     "-frames:v", "1", str(out)])  # fmt: skip
                og = cv2.imread(str(out), cv2.IMREAD_GRAYSCALE)
                table[name].append(
                    (
                        wc.psnr(og, tg),
                        wc.ssim(og, tg),
                        wc.detail(og) / max(wc.detail(tg), 1e-6),
                        halos(og, tg),
                    )
                )
            print(f"{login} t={t}: truth {cw}x{ch}, webcam size {lw}x{lh} (x{a.factor})", file=sys.stderr)
    print(
        f"{'chain':30s} {'PSNR':>6s} {'SSIM':>7s} {'detail':>7s} {'halos':>7s}   "
        f"(means over {len(a.heads)} heads)"
    )
    for name, rows in table.items():
        if rows:
            m = [statistics.fmean(r[i] for r in rows) for i in range(4)]
            print(f"{name:30s} {m[0]:6.2f} {m[1]:7.4f} {m[2]:7.2f} {100 * m[3]:6.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
