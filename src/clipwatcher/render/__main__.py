"""CLI: python -m clipwatcher.render --input X --start S --duration D ... --out F.mp4"""

from __future__ import annotations

import argparse
from pathlib import Path

from clipwatcher.render.captions import demo_words
from clipwatcher.render.template import render_clean, render_short


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipwatcher.render")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--start", required=True, type=float)
    ap.add_argument("--duration", required=True, type=float)
    ap.add_argument("--style", choices=["ambient", "gradient"], default=None)
    ap.add_argument("--accent", choices=["red", "yellow", "cyan"], default=None)
    ap.add_argument("--platform", choices=["youtube", "social"], default="youtube")
    ap.add_argument("--hook", default=None)
    ap.add_argument("--streamer", default="streamer")
    ap.add_argument("--clip-id", default="cli")
    ap.add_argument("--profile", default="cpu_batch")
    ap.add_argument("--demo-captions", action="store_true")
    ap.add_argument("--no-webcam", action="store_true")
    ap.add_argument("--clean", type=Path, default=None, help="also write the clean 16:9 clip here")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    words = demo_words(a.duration) if a.demo_captions else []
    render_short(
        a.input, a.start, a.duration, a.out, clip_id=a.clip_id, streamer=a.streamer, words=words,
        hook=a.hook, platform=a.platform, style=a.style, accent=a.accent, has_webcam=not a.no_webcam,
        profile=a.profile,
    )  # fmt: skip
    print(a.out)
    if a.clean:
        render_clean(a.input, a.start, a.duration, a.clean, profile=a.profile)
        print(a.clean)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
