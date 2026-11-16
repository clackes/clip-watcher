#!/usr/bin/env python3
"""Generate placeholder branding PNGs into assets/branding."""

from __future__ import annotations

import argparse
from pathlib import Path

from clipwatcher.render.branding import generate_all


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None, help="output dir (default assets/branding)")
    ap.add_argument("--hook", default=None, help="also render hook_pill.png with this text")
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args()
    for p in generate_all(a.out, hook=a.hook, overwrite=a.overwrite):
        print(p)


if __name__ == "__main__":
    main()
