"""Work the companion app does on a clip that is already staged. One job so far: render its Short again
from its own clean cut, with another caption style or layout.

    python -m clipwatcher.ui.jobs render <clip id> [--captions word|pop|classic|bounce] [--layout split|stack]

Only a clip nobody has decided on yet (READY) is rendered again, as with staging the same id twice (D7): a
claimed, approved, rejected or published clip keeps the video the operator saw.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from clipwatcher import config, events
from clipwatcher.queue.staging import atomic_write_json, staging_root
from clipwatcher.render import recipe
from clipwatcher.render.captions import CAPTION_STYLES, caption_style
from clipwatcher.ui import data

LAYOUTS = ("split", "stack")


def _duration(media: Path) -> float:
    out = subprocess.run(  # noqa: S603 - fixed arguments, the path is a staged clip's own file
        [
            config.ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(media),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(out.strip())


def _ready(clip_id: str, root: Path) -> dict[str, Any]:
    found = data.clip(clip_id, root)
    if found is None:
        raise ValueError("No such clip.")
    if found["state"] != "READY":
        raise ValueError(f"This clip is already {found['state'].lower()}: its video stays as it is.")
    return found


def rerender(
    clip_id: str, captions: str | None = None, layout: str | None = None, root: Path | None = None
) -> Path:
    """Render the Short of a staged clip again and put it in the place of the old one. Returns the new file.
    ValueError says, in words for the page, why a clip cannot be rendered again."""
    if captions is not None and captions not in CAPTION_STYLES:
        raise ValueError(f"Caption style: one of {', '.join(CAPTION_STYLES)}.")
    if layout is not None and layout not in LAYOUTS:
        raise ValueError(f"Layout: one of {', '.join(LAYOUTS)}.")
    root = Path(root) if root else staging_root()
    _ready(clip_id, root)
    folder = root / clip_id
    clean, short = folder / "clean_16x9.mp4", folder / "short_9x16.mp4"
    if not clean.is_file():
        raise ValueError("This clip has no clean cut to render from.")
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    streamer = str(meta.get("streamer") or "")
    dur = _duration(clean)
    style = captions or meta.get("captions") or caption_style(clip_id)
    render_cfg = config.global_config().get("render", {})
    layout = layout or meta.get("render_layout") or render_cfg.get("layout") or "split"

    kept = recipe.read(folder)
    if kept is None:  # staged before recipes were kept: work the words and the webcam out of the clean cut
        from clipwatcher import layout as scenes
        from clipwatcher.judge import transcribe

        with events.step("transcribe", clip_id=clip_id):
            words = transcribe.transcribe(clean, 0.0, dur, config.stream_language(streamer))
        with events.step("layout", clip_id=clip_id):
            lay = scenes.resolve(clean, streamer, peak_s=dur / 2)
        kept = {"words": words, "hook": meta.get("hook"), **lay.as_render_kwargs()}

    from clipwatcher.render.template import render_short

    new = folder / "short_9x16.new.mp4"
    try:
        with events.step("render_short", clip_id=clip_id, captions=style, layout=layout):
            render_short(
                clean,
                0.0,
                dur,
                new,
                clip_id=clip_id,
                streamer=streamer,
                captions=style,
                layout=layout,
                profile=str(render_cfg.get("profile") or "cpu_batch"),
                **kept,
            )
        with events.step("stage", clip_id=clip_id):
            _ready(clip_id, root)  # an operator may have claimed it while it rendered
            os.replace(new, short)
            meta.update(captions=style, render_layout=layout, rendered_again=round(time.time(), 1))
            if not kept["words"]:
                meta["captions"] = ""  # nothing was said, or no speech model: there are no captions on it
            atomic_write_json(folder / "meta.json", meta)
            if (root / "clips.db").exists():  # the queue: the new video gets a new card
                from clipwatcher.queue.db import ClipDB

                ClipDB(root / "clips.db").add_clip(clip_id, streamer, meta, float(meta.get("score", 0) or 0))
    finally:
        new.unlink(missing_ok=True)
    events.emit(
        "staged",
        streamer=streamer,
        clip_id=clip_id,
        path=str(folder),
        title=meta.get("title") or clip_id,
        hook=meta.get("hook") or "",
        final_score=round(float(meta.get("final_score") or 0), 2),
        seconds=round(dur, 2),
        timings={},
        again=True,
        captions=meta["captions"],
        layout=layout,
    )
    return short


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m clipwatcher.ui.jobs")
    sub = ap.add_subparsers(dest="job", required=True)
    r = sub.add_parser("render", help="render a staged clip's Short again")
    r.add_argument("clip_id")
    r.add_argument("--captions", choices=CAPTION_STYLES)
    r.add_argument("--layout", choices=LAYOUTS)
    args = ap.parse_args(argv)
    try:
        out = rerender(args.clip_id, args.captions, args.layout)
    except ValueError as e:
        print(f"not rendered: {e}")
        return 1
    except KeyboardInterrupt:  # Stop in the app: the half-made file is gone, the clip is as it was
        print("stopped: the clip keeps the video it had")
        return 130
    print(f"rendered {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
