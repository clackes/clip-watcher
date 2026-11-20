# ruff: noqa: E501
"""Gallery of the Remotion caption templates: contact sheets and demo clips over real footage.

For each template and colour mode (fixed, auto): a contact sheet of consecutive frames across an entrance,
an emphasised word and an exit, and a ten-second demo mp4, both composited over a real 1080x1920 video.
The footage has no transcript, so the words are a fixed demo script.

  python -m clipwatcher.render.rm_gallery --out DIR --videos a.mp4 b.mp4 [--templates punch,slam]

Needs Pillow, ffmpeg and the Remotion setup (render/remotion.py). Output is for looking at, never commit it.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from clipwatcher import config
from clipwatcher.models import Word
from clipwatcher.render import remotion as rm
from clipwatcher.render import rm_plan

DEMO_S = 10.0
# (start, end, word): two phrases, a pause (exit), fast talk, a shouted line, a pause before the label
DEMO_SCRIPT: tuple[tuple[float, float, str], ...] = (
    (0.30, 0.55, "NO"), (0.55, 0.95, "WAY!"),
    (1.05, 1.25, "did"), (1.25, 1.45, "you"), (1.45, 1.70, "see"), (1.70, 2.10, "that?"),
    (3.00, 3.20, "he"), (3.20, 3.45, "just"), (3.45, 3.75, "went"), (3.75, 4.00, "ALL"), (4.00, 4.40, "IN,"),
    (4.45, 4.62, "and"), (4.62, 4.78, "it"), (4.78, 5.10, "actually"), (5.10, 5.55, "worked!"),
    (6.40, 6.65, "that's"), (6.65, 6.95, "100%"), (6.95, 7.15, "the"), (7.15, 7.60, "craziest"), (7.60, 8.00, "play"),
    (8.05, 8.25, "I've"), (8.25, 8.50, "ever"), (8.50, 8.90, "seen"),
)  # fmt: skip
DEMO_BEATS = ((9.05, "hype", "*LET'S GO*"),)


def demo_words() -> list[Word]:
    return [Word(s, e, t) for s, e, t in DEMO_SCRIPT]


def demo_props(template: str = "punch", fps: float = 60.0) -> dict:
    """Props of the demo script in the seam box, fixed colours: remotion/demo/props.json (the default props
    of the composition, what Remotion Studio opens with). No emoji: the file is the same on every machine."""
    props, _ = rm.build_props(
        demo_words(),
        DEMO_S,
        template,
        box=rm.caption_box(960, "seam"),
        fps=fps,
        beats=list(DEMO_BEATS),
        colors="fixed",
    )
    return {**props, "emojiFamily": ""}


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-800:]!r}")
    return r


def video_fps(video: Path) -> float:
    r = _run(
        [config.ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(video)],
        text=True,
    )  # fmt: skip
    n, _, d = r.stdout.strip().partition("/")
    return float(n) / float(d or 1)


def make_overlay(
    template: str,
    video: Path,
    start: float,
    work: Path,
    *,
    colors: str,
    kind: str = "seam",
    cap_y: int = 960,
    fps: float | None = None,
):
    """Render the demo script over `video` from `start`. Returns (overlay, plan, box, props)."""
    fps = fps or min(60.0, video_fps(video))
    box = rm.caption_box(cap_y, kind)
    bg = None
    if colors == "auto":
        with tempfile.TemporaryDirectory(prefix="cw_gallery_") as td:
            cut = Path(td) / "cut.mp4"
            _run(
                [
                    config.ffmpeg_bin(),
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{start:.3f}",
                    "-t",
                    f"{DEMO_S}",
                    "-i",
                    str(video),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "12",
                    str(cut),
                ]
            )
            bg = rm.sample_video(cut, box)
    props, plan = rm.build_props(
        demo_words(), DEMO_S, template, box=box, fps=fps, beats=list(DEMO_BEATS), colors=colors, background=bg
    )
    ov = rm.render_props(props, work / f"{template}-{colors}", y=box.y0)
    return ov, plan, box, props


def demo_clip(ov: rm.Overlay, video: Path, start: float, out: Path) -> Path:
    """The ten seconds of `video` from `start` with the overlay on, composited as render_short does."""
    graph = f"[0:v]fps={ov.fps:g},format=yuv420p[base];" + rm.overlay_filter(1, ov, "[base]", "[v]")
    _run(
        [
            config.ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{DEMO_S}", "-i", str(video),
            *rm.overlay_input(ov), "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-r", f"{ov.fps:g}",
            "-c:a", "aac", "-b:a", "160k", "-t", f"{DEMO_S}", "-movflags", "+faststart", str(out),
        ]
    )  # fmt: skip
    return out


def moments(plan: rm_plan.Plan, fps: float, n: int = 12) -> list[tuple[str, list[int]]]:
    """Three runs of n consecutive frames: an entrance, an emphasised word, an exit."""
    pages = plan.pages
    if not pages:
        return []
    first = round(pages[0].start * fps)
    out = [("entrance", list(range(max(0, first - 1), max(0, first - 1) + n)))]
    emph = [w for p in pages[1:] for w in p.words if w.emph] or [w for p in pages for w in p.words if w.emph]
    if emph:
        f = round(emph[0].start * fps)
        out.append((f"emphasis: {emph[0].text}", list(range(max(0, f - 2), max(0, f - 2) + n))))
    leaving = [p for p in pages if p.exit > 0]
    if leaving:
        end = round(leaving[0].end * fps)
        out.append(("exit", list(range(max(0, end - n + 2), max(0, end - n + 2) + n))))
    return out


def contact_sheet(
    ov: rm.Overlay,
    plan: rm_plan.Plan,
    video: Path,
    start: float,
    out: Path,
    *,
    margin: int = 150,
    scale: float = 0.5,
    title: str = "",
) -> Path:
    """Consecutive frames of the three moments over the real picture: one row per moment."""
    from PIL import Image, ImageDraw

    runs = moments(plan, ov.fps)
    frames = sorted({f for _, fs in runs for f in fs if f < ov.frames})
    if not frames:
        raise RuntimeError("nothing to show")
    y0 = max(0, ov.y - margin) // 2 * 2
    y1 = min(rm.FRAME_H, ov.y + ov.height + margin)
    with tempfile.TemporaryDirectory(prefix="cw_sheet_") as td:
        sel = "+".join(f"eq(n\\,{f})" for f in frames)
        _run(
            [
                config.ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{DEMO_S}", "-i", str(video),
                "-vf", f"fps={ov.fps:g},select='{sel}',crop={rm.FRAME_W}:{y1 - y0}:0:{y0}", "-fps_mode", "passthrough",
                f"{td}/%04d.png",
            ]
        )  # fmt: skip
        tiles: dict[int, Image.Image] = {}
        for i, f in enumerate(frames):
            base = Image.open(f"{td}/{i + 1:04d}.png").convert("RGBA")
            with Image.open(ov.dir / f"{f:06d}.png") as layer:
                base.alpha_composite(layer.convert("RGBA"), (ov.x, ov.y - y0))
            w, h = round(base.width * scale), round(base.height * scale)
            tiles[f] = base.convert("RGB").resize((w, h), Image.LANCZOS)
    w, h = next(iter(tiles.values())).size
    n = max(len(fs) for _, fs in runs)
    cols = (n + 1) // 2
    head = 26
    rows = sum((len(fs) + cols - 1) // cols for _, fs in runs)
    sheet = Image.new("RGB", (cols * (w + 4) + 4, rows * (h + 4) + len(runs) * head + 34), "#0D0D12")
    draw = ImageDraw.Draw(sheet)
    draw.text((8, 8), title or out.stem, fill="#FFFFFF")
    y = 34
    for name, fs in runs:
        draw.text((8, y + 6), f"{name}   frames {fs[0]}-{fs[-1]} at {ov.fps:g} fps", fill="#B8B8C8")
        y += head
        for i, f in enumerate(fs):
            if f in tiles:
                sheet.paste(tiles[f], (4 + (i % cols) * (w + 4), y + (i // cols) * (h + 4)))
        y += ((len(fs) + cols - 1) // cols) * (h + 4)
    sheet.save(out)
    return out


def build(
    templates: list[str],
    videos: list[Path],
    out: Path,
    *,
    start: float = 4.0,
    modes=("fixed", "auto"),
    kind: str = "seam",
    cap_y: int = 960,
    keep_frames: bool = False,
) -> list[dict]:
    """The whole gallery: per template and colour mode a sheet and a demo clip, plus index.html."""
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_frames"
    made = []
    for i, name in enumerate(templates):
        video = videos[i % len(videos)]
        params = rm.load_template(name)
        row = {
            "template": name,
            "title": params["title"],
            "description": params["description"],
            "video": video.parent.name,
        }
        for mode in modes:
            ov, plan, _, _ = make_overlay(name, video, start, work, colors=mode, kind=kind, cap_y=cap_y)
            sheet = contact_sheet(
                ov,
                plan,
                video,
                start,
                out / f"{name}_{mode}.png",
                title=f"{name} - {mode} colours - over {video.parent.name}",
            )
            clip = demo_clip(ov, video, start, out / f"{name}_{mode}.mp4")
            row[mode] = {
                "sheet": sheet.name,
                "clip": clip.name,
                "render_s": round(ov.seconds, 2),
                "frames": ov.frames,
            }
            if not keep_frames:
                shutil.rmtree(ov.dir, ignore_errors=True)
        made.append(row)
        print(f"{name}: " + ", ".join(f"{m} {row[m]['render_s']}s" for m in modes), flush=True)
    shutil.rmtree(work, ignore_errors=True)
    (out / "gallery.json").write_text(json.dumps(made, indent=1), encoding="utf-8")
    cards = []
    for r in made:
        cells = "".join(
            f"<figure><figcaption>{m}</figcaption><video src='{r[m]['clip']}' controls loop muted playsinline></video>"
            f"<a href='{r[m]['sheet']}'><img src='{r[m]['sheet']}' loading='lazy'></a></figure>"
            for m in modes
        )
        cards.append(
            f"<section><h2>{html.escape(r['template'])}</h2><p>{html.escape(r['description'])}</p><div class='row'>{cells}</div></section>"
        )
    page = (
        "<!doctype html><meta charset='utf-8'><title>Caption templates</title><style>body{background:#0d0d12;color:#eee;"
        "font:15px system-ui;margin:24px}section{margin:0 0 44px}h2{margin:0 0 4px}p{color:#aab;margin:0 0 12px;max-width:900px}"
        ".row{display:flex;gap:16px;flex-wrap:wrap}figure{margin:0;display:flex;gap:10px;align-items:flex-start}"
        "figcaption{writing-mode:vertical-rl;color:#889}video{height:420px;border-radius:8px}img{height:420px;border-radius:8px}"
        f"</style><h1>Caption templates ({len(made)})</h1>" + "".join(cards)
    )
    (out / "index.html").write_text(page, encoding="utf-8")
    return made


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Contact sheets and demo clips of the caption templates over real footage."
    )
    ap.add_argument("--out")
    ap.add_argument("--write-demo", action="store_true", help="rewrite remotion/demo/props.json and stop")
    ap.add_argument(
        "--videos", nargs="+", default=None, help="1080x1920 videos to composite over (used in turn)"
    )
    ap.add_argument("--templates", default="all")
    ap.add_argument("--start", type=float, default=4.0, help="where in the videos the ten seconds start")
    ap.add_argument("--kind", default="seam", help="caption box: seam | frame | reframe | stack")
    ap.add_argument("--cap-y", type=int, default=960)
    a = ap.parse_args(argv)
    if a.write_demo:
        text = json.dumps(demo_props(), indent=1) + "\n"
        (rm.REMOTION_DIR / "demo" / "props.json").write_text(text, encoding="utf-8")
        return 0
    if not a.out or not a.videos:
        ap.error("--out and --videos are required")
    why = rm.missing()
    if why:
        print(f"Remotion is not set up: {why}", file=sys.stderr)
        return 2
    names = rm.templates() if a.templates == "all" else a.templates.split(",")
    build(names, [Path(v) for v in a.videos], Path(a.out), start=a.start, kind=a.kind, cap_y=a.cap_y)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
