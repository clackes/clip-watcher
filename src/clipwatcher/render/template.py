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
from clipwatcher.render.captions import caption_style, write_ass
from clipwatcher.render.cold_open import settings as co_settings
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


def hook_seconds() -> float:
    return float(config.global_config().get("render", {}).get("hook_s", 3.5))


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


LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"


def _audio_chain(
    trim: float, dur: float, cold_open: tuple[float, float] | None = None, measured: dict | None = None
) -> str:
    """Audio graph ending in [a]. measured = loudnorm pass-1 stats: one fixed gain + limiter instead of
    loudnorm's dynamic mode, which rides the gain on the first seconds (a loud cold open came out garbled)."""
    if measured:  # fixed gain to -14 LUFS + true-peak-ish limiter at -1.5 dBFS (0.841)
        gain = max(-30.0, min(30.0, -14.0 - float(measured["input_i"])))
        norm = f"volume={gain:.2f}dB,alimiter=limit=0.841:level=false:latency=true"
    else:
        norm = LOUDNORM
    norm += ",aresample=48000[a]"
    if not cold_open:
        return f"[0:a]atrim=start={trim:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,{norm}"
    cs, cd = cold_open
    return (
        "[0:a]asplit=2[coa][mna];"
        f"[coa]atrim=start={trim + cs:.3f}:duration={cd:.3f},asetpts=PTS-STARTPTS,"
        f"afade=t=out:st={cd - 0.06:.3f}:d=0.06[coa1];"
        f"[mna]atrim=start={trim:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.04[mna1];"
        f"[coa1][mna1]concat=n=2:v=0:a=1,{norm}"
    )


def _measure(src: Path, coarse: float, trim: float, dur: float, cold_open=None) -> dict | None:
    """loudnorm pass 1 over the exact audio the render will use; None on failure (dynamic mode then)."""
    chain = _audio_chain(trim, dur, cold_open).replace(
        f"{LOUDNORM},aresample=48000[a]", f"{LOUDNORM}:print_format=json[a]"
    )
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-ss", f"{coarse:.3f}", "-i", str(src)]
    cmd += ["-filter_complex", chain, "-map", "[a]", "-f", "null", "-"]
    try:
        err = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stderr
        m = json.loads(err[err.rindex("{") : err.rindex("}") + 1])
        return m if m["input_i"] not in ("-inf", "inf") else None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


PANEL_Y, PANEL_H = 270, 1020  # area between the top banner and the CTA


def _cold_open_panel(cam: Rect | None, src_wh: tuple[int, int], face: Rect | None = None) -> Rect:
    """Source crop shown during the cold open: only the webcam, filling the 1080x1020 panel.

    As tall as the facecam, centred on the face when known (else on the cam), kept inside the cam
    horizontally where it fits. Without a webcam the full frame is used (letterboxed).
    """
    fw, fh = src_wh
    if not cam:
        return 0, 0, fw, fh
    cx, cy, cw, ch = cam
    h = min(fh, ch)
    w = min(fw, h * 1080 / PANEL_H)
    h = min(h, w * PANEL_H / 1080)
    w, h = int(w) // 2 * 2, int(h) // 2 * 2
    mx, my = cx + cw / 2, cy + ch / 2
    if face:
        mx, my = face[0] + face[2] / 2, face[1] + face[3] / 2
        mx = min(max(mx, cx + w / 2), cx + cw - w / 2) if w <= cw else mx
        my = min(max(my, cy + h / 2), cy + ch - h / 2) if h <= ch else my
    x = min(max(0, mx - w / 2), fw - w)
    y = min(max(0, my - h / 2), fh - h)
    return int(x) // 2 * 2, int(y) // 2 * 2, w, h


def _cold_open_graph(
    src: str, *, has_webcam: bool, cam: Rect, src_wh: tuple[int, int], bg: str, use_bg: bool,
    zoom: float, fps: float, face: Rect | None = None,
) -> list[str]:  # fmt: skip
    """[src] (the cold-open frames) -> [cocomp]: 1080x1920 webcam-only frame with a punch-in zoom."""
    x, y, w, h = _cold_open_panel(cam if has_webcam else None, src_wh, face)
    pw, ph = (1080, PANEL_H) if has_webcam else (1080, 608)
    py = PANEL_Y if has_webcam else PANEL_Y + (PANEL_H - 608) // 2
    g: list[str] = []
    if use_bg:
        g.append(f"{src}split=2[cobs][cops]")
        g.append(
            "[cobs]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
            "scale=1080:1920,eq=brightness=-0.22:saturation=0.85,setsar=1[cobg]"
        )
        src, bg = "[cops]", "[cobg]"
    # zoompan on a 2x canvas so the ramp does not jitter on whole-pixel steps
    z = f"min(1+{zoom - 1:.3f}*in/{max(1.0, 0.35 * fps):.2f},{zoom:.3f})"
    zp = (
        f",scale={pw * 2}:{ph * 2}:flags=lanczos,zoompan=z='{z}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'"
        f":d=1:s={pw}x{ph}:fps={fps:g},setpts=PTS-STARTPTS"
        if zoom > 1.0
        else f",scale={pw}:{ph}:flags=lanczos"
    )
    g.append(f"{src}crop={w}:{h}:{x}:{y}{zp},setsar=1[copanel]")
    g.append(f"{bg}[copanel]overlay=0:{py}:shortest=1,format=yuv420p,setsar=1[cocomp]")
    return g


def build_short_graph(
    *, trim: float, dur: float, style: str, has_webcam: bool, has_hook: bool, cam: Rect, game: Rect,
    gradient_idx: int, cta_idx: int, logo_idx: int, hook_idx: int, ass: Path, fontsdir: Path,
    hook_s: float = 3.5, cold_open: tuple[float, float] | None = None, zoom: float = 1.15,
    measured: dict | None = None, src_wh: tuple[int, int] = (1920, 1080), fps: float = 30.0,
    face: Rect | None = None,
) -> str:  # fmt: skip
    g: list[str] = []
    use_bg = style == "ambient"
    grad = f"[{gradient_idx}:v]"
    if cold_open:  # webcam-only replay of the reaction, then the clip from its start
        cs, cd = cold_open
        g.append(
            "[0:v]split=2[cov][mnv];"
            f"[cov]trim=start={trim + cs:.3f}:duration={cd:.3f},setpts=PTS-STARTPTS[cov1];"
            f"[mnv]trim=start={trim:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS[srcv]"
        )
        trimmed = "[srcv]null"
        if not use_bg:
            g.append(f"{grad}split=2[gmain][gco]")
            grad = "[gmain]"
        g += _cold_open_graph(
            "[cov1]", has_webcam=has_webcam, cam=cam, src_wh=src_wh, bg="[gco]", use_bg=use_bg, zoom=zoom,
            fps=fps, face=face,
        )  # fmt: skip
    else:
        trimmed = f"[0:v]trim=start={trim:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS"
    outs = (["bgs"] if use_bg else []) + (["cs", "gs"] if has_webcam else ["fs"])
    g += [
        f"{trimmed},split={len(outs)}" + "".join(f"[{o}]" for o in outs)
        if len(outs) > 1
        else f"{trimmed}[{outs[0]}]"
    ]
    if use_bg:
        g.append(
            "[bgs]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
            "scale=1080:1920,eq=brightness=-0.22:saturation=0.85,setsar=1[bg]"
        )
        last = "[bg]"
    else:
        last = grad
    sh = ":shortest=1" if not use_bg else ""
    if has_webcam:
        cx, cy, cw, ch = cam
        gx, gy, gw, gh = game
        g.append(f"[cs]crop={cw}:{ch}:{cx}:{cy},scale=1080:480:flags=lanczos,setsar=1[cam]")
        g.append(f"[gs]crop={gw}:{gh}:{gx}:{gy},scale=1080:540:flags=lanczos,setsar=1[game]")
        g.append(f"{last}[cam]overlay=0:{PANEL_Y}{sh}[v0]")
        g.append("[v0][game]overlay=0:750[v1m]")
    else:
        g.append("[fs]scale=1080:608:flags=lanczos,setsar=1[full]")
        g.append(f"{last}[full]overlay=0:{PANEL_Y + (PANEL_H - 608) // 2}{sh}[v1m]")
    if cold_open:
        g.append("[v1m]format=yuv420p,setsar=1[mcomp];[cocomp][mcomp]concat=n=2:v=1:a=0[v1]")
    else:
        g.append("[v1m]null[v1]")
    g.append(f"[v1][{logo_idx}:v]overlay=(W-w)/2:96[v2]")
    g.append(f"[v2][{cta_idx}:v]overlay=(W-w)/2:1300[v3]")
    if has_hook:
        h0 = cold_open[1] if cold_open else 0.0  # no centre text over the cold open (owner, 17 Nov)
        g.append(f"[v3][{hook_idx}:v]overlay=(W-w)/2:702:enable='between(t,{h0:.3f},{h0 + hook_s:.3f})'[v4]")
    else:
        g.append("[v3]null[v4]")
    g.append(
        f"[v4]ass=filename={esc_filter_path(ass)}:fontsdir={esc_filter_path(fontsdir)},format=yuv420p[v]"
    )
    g.append(_audio_chain(trim, dur, cold_open, measured))
    return ";".join(g)


def render_short(
    src: Path, start_s: float, dur_s: float, out: Path, *, clip_id: str, streamer: str,
    words: list[Word], hook: str | None, platform: str = "youtube", style: str | None = None,
    accent: str | None = None, cam: Rect | None = None, game: Rect | None = None,
    has_webcam: bool = True, profile: str = "cpu_batch", captions: str | None = None,
    cold_open: tuple[float, float] | None = None, face: Rect | str | None = None,
) -> Path:  # fmt: skip
    """cold_open=(start, duration) relative to start_s: that moment plays first, zoomed in, then the clip.
    The output is duration + dur_s long; captions follow the clip part."""
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
    co_d = cold_open[1] if cold_open else 0.0
    total = dur_s + co_d
    if cold_open:  # captions for the replayed shout (sound-off viewers), then the clip re-timed after it
        cs = cold_open[0]
        co_words = [
            Word(max(0.0, w.start - cs), min(co_d, w.end - cs), w.text)
            for w in words
            if cs - 0.05 <= w.start < cs + co_d - 0.1
        ]
        words = co_words + [Word(w.start + co_d, w.end + co_d, w.text) for w in words]

    with tempfile.TemporaryDirectory(prefix="clipwatcher_") as td:
        work = Path(td)
        ass = write_ass(
            work / "subs.ass", words, total, credit=config.credit(streamer),
            style=captions or caption_style(clip_id),
        )  # fmt: skip
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
            ass=ass, fontsdir=FONTS_DIR, hook_s=hook_seconds(), cold_open=cold_open,
            zoom=co_settings()[1], measured=_measure(src, coarse, trim, dur_s, cold_open),
            src_wh=(info.width, info.height), fps=fps, face=parse_rect(face),
        )  # fmt: skip
        cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", *video_args(profile)]
        cmd += ["-fps_mode", "cfr", "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        cmd += ["-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
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
        + _audio_chain(trim, dur_s, measured=_measure(src, coarse, trim, dur_s))
    )
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-ss", f"{coarse:.3f}", "-i", str(src)]
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", *video_args(profile)]
    cmd += ["-fps_mode", "cfr", "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-t", f"{dur_s:.3f}", "-movflags", "+faststart", str(out)]
    _run(cmd)
    return out
