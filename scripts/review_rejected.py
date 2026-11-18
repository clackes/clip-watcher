"""Audit rejected live clips (fastlane.keep_rejected): what the judge saw vs what it said.

    python scripts/review_rejected.py [N]      # the N most recent rejected clips (default 6)

For each kept clip writes work/fastlane/<clip_id>/sheet.jpg (the judge's frames with their time, the chat
peak marked) and prints the judge's score/reason, the transcript and the chat that reacted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from clipwatcher import config
from clipwatcher.judge.compact import compact_chat
from clipwatcher.judge.frames import frame_times

ROOT = config.ROOT / "work" / "fastlane"


def sheet(d: Path, dur: float, peak_rel: float) -> Path | None:
    frames = sorted((d / "frames").glob("f*.jpg"))
    if not frames:
        return None
    cols, w, h = 4, 480, 270
    rows = (len(frames) + cols - 1) // cols
    img = Image.new("RGB", (cols * w, rows * (h + 22)), "black")
    draw = ImageDraw.Draw(img)
    times = frame_times(dur, len(frames), peak_rel)  # same deterministic times the judge's frames used
    for i, f in enumerate(frames):
        x, y = (i % cols) * w, (i // cols) * (h + 22)
        img.paste(Image.open(f).resize((w, h)), (x, y + 22))
        t = times[i] if i < len(times) else None
        label = f"{f.stem}" + (f"  t={t:.1f}s" if t is not None else "")
        if t is not None and abs(t - peak_rel) < 1.6:
            label += "  <- chat peak"
        draw.text((x + 6, y + 4), label, fill="yellow")
    out = d / "sheet.jpg"
    img.save(out, quality=80)
    return out


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    kept = [p for p in ROOT.iterdir() if (p / "review.json").exists()]
    dirs = sorted(kept, key=lambda p: p.stat().st_mtime)
    for d in dirs[-n:]:
        r = json.loads((d / "review.json").read_text(encoding="utf-8"))
        c, j = r["candidate"], r.get("judge") or {}
        peak_rel = c["peak_s"] - c["start_s"]
        dur = c["end_s"] - c["start_s"]
        s = sheet(d, dur, peak_rel)
        words = " ".join(w["text"] for w in r.get("words", []))
        ch = compact_chat(r.get("chat", []))
        print(f"== {d.name}  chat_score={c['chat_score']:.1f}  peak at {peak_rel:.0f}s of {dur:.0f}s")
        why = j.get("why") or j.get("reject_reason")
        print(f"   judge sc={j.get('score')} ok={j.get('ok')} flags={j.get('flags')} why: {why}")
        print(f"   judge window {j.get('trim_start_s')}-{j.get('trim_end_s')} pk={j.get('peak_rel_s')}")
        print(f"   transcript: {words[:300]}")
        print(f"   chat n={ch['n']} u={ch['u']} top={ch['top'][:8]}")
        print(f"   sheet: {s}")


if __name__ == "__main__":
    main()
