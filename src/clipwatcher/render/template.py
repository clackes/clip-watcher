"""One-pass ffmpeg render of the 9:16 template (D5/D4/D9) and the clean 16:9 export."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from clipwatcher import config
from clipwatcher.config import ffmpeg_bin, ffprobe_bin
from clipwatcher.models import Word
from clipwatcher.render.branding import ACCENTS, BRANDING_DIR, FONTS_DIR, ensure_branding, hook_pill
from clipwatcher.render.captions import write_ass
from clipwatcher.render.encoders import video_args

STYLES = ("ambient", "gradient")
Rect = tuple[int, int, int, int]  # x, y, w, h in source pixels


@dataclass
class SourceInfo:
    width: int
    height: int
    fps: float


def probe(src: Path) -> SourceInfo:
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate", "-of", "json", str(src)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    s = json.loads(r.stdout)["streams"][0]
    n, _, d = s["r_frame_rate"].partition("/")
    fps = float(n) / float(d or 1)
    return SourceInfo(int(s["width"]), int(s["height"]), fps if fps > 0 else 30.0)


def stable_hash(clip_id: str) -> int:
    return int(hashlib.sha256(clip_id.encode()).hexdigest(), 16)


def pick_style(clip_id: str) -> tuple[str, str]:
    h = stable_hash(clip_id)
    return STYLES[h % 2], list(ACCENTS)[(h // 2) % 3]


def parse_rect(r: Rect | str | None) -> Rect | None:
    """Accept a Rect (x, y, w, h) or an ffmpeg crop string "w:h:x:y" as stored in streamers.yaml."""
    if r is None or isinstance(r, tuple):
        return r
    w, h, x, y = (int(float(v)) for v in str(r).split(":"))
    return (x, y, w, h)


def default_cam(info: SourceInfo) -> Rect:
    w = int(info.width * 0.3) // 2 * 2
    h = int(w / 2.25) // 2 * 2
    return 0, info.height - h, w, h


def default_game(info: SourceInfo) -> Rect:
    h = int(info.width / 2) // 2 * 2
    h = min(h, info.height)
    return 0, (info.height - h) // 2 // 2 * 2, info.width, h


def esc_filter_path(p: str | Path) -> str:
    out = []
    for ch in str(p):
        if ch in "\\:'[],;":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _seek(start_s: float) -> tuple[float, float]:
    coarse = max(0.0, start_s - 6)
    return coarse, start_s - coarse


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({r.returncode}): {r.stderr[-2000:]}")


def _audio_chain(trim: float, dur: float) -> str:
    return (
        f"[0:a]atrim=start={trim:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,"
        "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
    )


def build_short_graph(
    *, trim: float, dur: float, style: str, has_webcam: bool, has_hook: bool, cam: Rect, game: Rect,
    gradient_idx: int, cta_idx: int, logo_idx: int, hook_idx: int, ass: Path, fontsdir: Path,
) -> str:  # fmt: skip
    trimmed = f"[0:v]trim=start={trim:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS"
    use_bg = style == "ambient"
    outs = (["bgs"] if use_bg else []) + (["cs", "gs"] if has_webcam else ["fs"])
    g = [
        f"{trimmed},split={len(outs)}" + "".join(f"[{o}]" for o in outs)
        if len(outs) > 1
        else f"{trimmed}[{outs[0]}]"
    ]
    if use_bg:
        g.append(
            "[bgs]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
            "scale=1080:1920,eq=brightness=-0.22:saturation=0.85,setsar=1[bg]"
        )
        base = "bg"
    else:
        base = f"{gradient_idx}:v"
    last = f"[{base}]"
    if has_webcam:
        cx, cy, cw, ch = cam
        gx, gy, gw, gh = game
        g.append(f"[cs]crop={cw}:{ch}:{cx}:{cy},scale=1080:480:flags=lanczos,setsar=1[cam]")
        g.append(f"[gs]crop={gw}:{gh}:{gx}:{gy},scale=1080:540:flags=lanczos,setsar=1[game]")
        sh = ":shortest=1" if not use_bg else ""
        g.append(f"{last}[cam]overlay=0:270{sh}[v0]")
        g.append("[v0][game]overlay=0:750[v1]")
    else:
        g.append("[fs]scale=1080:608:flags=lanczos,setsar=1[full]")
        sh = ":shortest=1" if not use_bg else ""
        g.append(f"{last}[full]overlay=0:{270 + (1020 - 608) // 2}{sh}[v1]")
    g.append(f"[v1][{logo_idx}:v]overlay=140:150[v2]")
    g.append(f"[v2][{cta_idx}:v]overlay=120:1410[v3]")
    if has_hook:
        g.append(f"[v3][{hook_idx}:v]overlay=(W-w)/2:702:enable='lt(t,2.5)'[v4]")
    else:
        g.append("[v3]null[v4]")
    g.append(
        f"[v4]ass=filename={esc_filter_path(ass)}:fontsdir={esc_filter_path(fontsdir)},format=yuv420p[v]"
    )
    g.append(_audio_chain(trim, dur))
    return ";".join(g)


def render_short(
    src: Path, start_s: float, dur_s: float, out: Path, *, clip_id: str, streamer: str,
    words: list[Word], hook: str | None, platform: str = "youtube", style: str | None = None,
    accent: str | None = None, cam: Rect | None = None, game: Rect | None = None,
    has_webcam: bool = True, profile: str = "cpu_batch",
) -> Path:  # fmt: skip
    src, out = Path(src), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    auto_style, auto_accent = pick_style(clip_id)
    style = style or auto_style
    accent = accent or auto_accent
    if style not in STYLES or accent not in ACCENTS:
        raise ValueError(f"bad style/accent: {style}/{accent}")
    info = probe(src)
    cam = parse_rect(cam) or default_cam(info)
    game = parse_rect(game) or default_game(info)
    coarse, trim = _seek(start_s)
    ensure_branding(BRANDING_DIR)
    cta = BRANDING_DIR / ("cta_youtube.png" if platform == "youtube" else "cta_social.png")
    fps = min(info.fps, 60.0)

    with tempfile.TemporaryDirectory(prefix="clipwatcher_") as td:
        work = Path(td)
        ass = write_ass(work / "subs.ass", words, dur_s, credit=config.credit(streamer))
        cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-ss", f"{coarse:.3f}", "-i", str(src)]
        idx = 1
        logo_idx = idx
        cmd += ["-i", str(BRANDING_DIR / "top_logo.png")]
        idx += 1
        cta_idx = idx
        cmd += ["-i", str(cta)]
        idx += 1
        hook_idx = -1
        if hook:
            hook_pill(hook, work / "hook_pill.png")
            hook_idx = idx
            cmd += ["-i", str(work / "hook_pill.png")]
            idx += 1
        gradient_idx = -1
        if style == "gradient":
            gradient_idx = idx
            cmd += [
                "-loop",
                "1",
                "-framerate",
                f"{fps:g}",
                "-i",
                str(BRANDING_DIR / f"frame_gradient_{accent}.png"),
            ]
            idx += 1
        graph = build_short_graph(
            trim=trim, dur=dur_s, style=style, has_webcam=has_webcam, has_hook=bool(hook), cam=cam,
            game=game, gradient_idx=gradient_idx, cta_idx=cta_idx, logo_idx=logo_idx, hook_idx=hook_idx,
            ass=ass, fontsdir=FONTS_DIR,
        )  # fmt: skip
        cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", *video_args(profile)]
        cmd += ["-fps_mode", "cfr", "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        cmd += ["-t", f"{dur_s:.3f}", "-movflags", "+faststart", str(out)]
        _run(cmd)
    return out


def render_clean(src: Path, start_s: float, dur_s: float, out: Path, *, profile: str = "cpu_batch") -> Path:
    src, out = Path(src), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    coarse, trim = _seek(start_s)
    fps = min(info.fps, 60.0)
    graph = (
        f"[0:v]trim=start={trim:.3f}:duration={dur_s:.3f},setpts=PTS-STARTPTS,format=yuv420p[v];"
        + _audio_chain(trim, dur_s)
    )
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-ss", f"{coarse:.3f}", "-i", str(src)]
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", *video_args(profile)]
    cmd += ["-fps_mode", "cfr", "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-t", f"{dur_s:.3f}", "-movflags", "+faststart", str(out)]
    _run(cmd)
    return out
