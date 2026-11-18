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


# 9:16 stack (owner, 17 Nov: no subscribe/like/comment banner). Captions stay at y1495, the lowest spot
# clear of the Shorts UI; logo, panel and hook moved down 125 px to close the gap the banner left.
STACK_SHIFT = 125
LOGO_Y = 96 + STACK_SHIFT
PANEL_Y, PANEL_H = 270 + STACK_SHIFT, 1020  # webcam + gameplay (or the cold open / no-webcam frame)
GAME_Y = PANEL_Y + 480
HOOK_Y = PANEL_Y + 432  # hook pill over the webcam/gameplay seam


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
    gradient_idx: int, logo_idx: int, hook_idx: int, ass: Path, fontsdir: Path, cta_idx: int = -1,
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
        g.append(f"[v0][game]overlay=0:{GAME_Y}[v1m]")
    else:
        g.append("[fs]scale=1080:608:flags=lanczos,setsar=1[full]")
        g.append(f"{last}[full]overlay=0:{PANEL_Y + (PANEL_H - 608) // 2}{sh}[v1m]")
    if cold_open:
        g.append("[v1m]format=yuv420p,setsar=1[mcomp];[cocomp][mcomp]concat=n=2:v=1:a=0[v1]")
    else:
        g.append("[v1m]null[v1]")
    g.append(f"[v1][{logo_idx}:v]overlay=(W-w)/2:{LOGO_Y}[v3]")
    if has_hook:
        h0 = cold_open[1] if cold_open else 0.0  # no centre text over the cold open (owner, 17 Nov)
        g.append(
            f"[v3][{hook_idx}:v]overlay=(W-w)/2:{HOOK_Y}:enable='between(t,{h0:.3f},{h0 + hook_s:.3f})'[v4]"
        )
    else:
        g.append("[v3]null[v4]")
    g.append(
        f"[v4]ass=filename={esc_filter_path(ass)}:fontsdir={esc_filter_path(fontsdir)},format=yuv420p[v]"
    )
    g.append(_audio_chain(trim, dur, cold_open, measured))
    return ";".join(g)


# -- split layout (owner, 17 Nov evening): full-bleed webcam over gameplay, 1080x960 each, captions on the
# seam, no on-screen hook. No webcam -> the full 16:9 frame over a blurred copy, captions under it.
LAYOUTS = ("split", "stack")
SEAM_Y = 960
FRAME_H = 760  # no-webcam frame 1080x760 (16:9 source, sides trimmed a little) -> y580-1340
FRAME_Y = 580
FRAME_CAP_Y = 1450
SPLIT_LOGO_W, SPLIT_LOGO_Y = 520, 40
PUNCH, PUNCH_S = 0.12, 0.45  # zoom punch on a beat: +12 %, eased out over 0.45 s
FX_FILTERS = {  # beat kind -> (seconds, filter) applied to the whole picture under the captions
    "fail": (0.7, "colorchannelmixer=rr=1:rg=0.35:rb=0.35:gr=0:gg=0.4:gb=0:br=0:bg=0:bb=0.4"),
    "shock": (1.6, "hue=s=0"),
    "hype": (0.3, "colorchannelmixer=rr=0.55:gr=0.3:gg=1.25:gb=0.3:bb=0.55"),
}


SFX_LIMIT = 0.891  # -1 dBFS ceiling after the sound effects are mixed in


def pick_sfx(beats: list[tuple]) -> list[tuple[float, Path]]:
    """(start time, file) for every beat whose sound the judge chose (render.sfx). Never random: no sound
    when the judge left sx empty. The sound starts early by its attack so the hit lands on the beat."""
    from clipwatcher import sfx

    if not config.global_config().get("render", {}).get("sfx", True):
        return []
    out = []
    for b in beats:
        name = b[3] if len(b) > 3 else ""
        f = sfx.path(name) if name else None
        if f:
            out.append((max(0.0, b[0] - sfx.attack(name)), f))
    return out


def _sfx_mix(sfx: list[tuple[float, int]], gain_db: float) -> str:
    """[am] + one input per (t, input index) -> [a]: delayed, mixed without rescaling, limited."""
    g, labels = [], []
    for j, (t, idx) in enumerate(sfx):
        ms = max(0, round(t * 1000))
        g.append(f"[{idx}:a]aresample=48000,volume={gain_db:.1f}dB,adelay={ms}|{ms}[sfx{j}]")
        labels.append(f"[sfx{j}]")
    g.append(
        f"[am]{''.join(labels)}amix=inputs={len(sfx) + 1}:duration=first:normalize=0,"
        f"alimiter=limit={SFX_LIMIT}:level=false,aresample=48000[a]"
    )
    return ";".join(g)


def layout_mode() -> str:
    m = str(config.global_config().get("render", {}).get("layout", "split"))
    return m if m in LAYOUTS else "split"


def fit_crop(rect: Rect, aspect: float, centre: tuple[float, float] | None = None) -> Rect:
    """Largest crop of aspect w/h inside rect, centred on centre (else the rect centre), even sizes."""
    x, y, w, h = rect
    cw, ch = (h * aspect, h) if w / h > aspect else (w, w / aspect)
    cw, ch = int(cw) // 2 * 2, int(ch) // 2 * 2
    mx, my = centre or (x + w / 2, y + h / 2)
    cx = min(max(mx - cw / 2, x), x + w - cw)
    cy = min(max(my - ch / 2, y), y + h - ch)
    return int(cx) // 2 * 2, int(cy) // 2 * 2, cw, ch


def zoom_expr(cold_open_s: float, zoom: float, beat_ts: list[float]) -> str | None:
    """zoompan z(it): cold-open punch-in ramp, then a short punch on every beat. None = no zoom."""
    terms = []
    if cold_open_s > 0 and zoom > 1:
        terms.append(f"{zoom - 1:.3f}*min(it/0.35,1)*lt(it,{cold_open_s:.3f})")
    for t in beat_ts:
        terms.append(f"{PUNCH}*between(it,{t:.3f},{t + PUNCH_S:.3f})*(1-(it-{t:.3f})/{PUNCH_S})")
    return "1+" + "+".join(terms) if terms else None


def _zoomed(src: str, crop: Rect, size: tuple[int, int], z: str | None, fps: float, out: str) -> str:
    x, y, w, h = crop
    pw, ph = size
    if not z:
        return f"{src}crop={w}:{h}:{x}:{y},scale={pw}:{ph}:flags=lanczos,setsar=1{out}"
    return (  # 2x canvas so slow ramps do not jitter on whole-pixel steps
        f"{src}crop={w}:{h}:{x}:{y},scale={pw * 2}:{ph * 2}:flags=lanczos,"
        f"zoompan=z='{z}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={pw}x{ph}:fps={fps:g},"
        f"setpts=PTS-STARTPTS,setsar=1{out}"
    )


def build_split_graph(
    *, trim: float, dur: float, has_webcam: bool, cam: Rect, game: Rect, logo_idx: int, ass: Path,
    fontsdir: Path, cold_open: tuple[float, float] | None = None, zoom: float = 1.15,
    measured: dict | None = None, src_wh: tuple[int, int] = (1920, 1080), fps: float = 30.0,
    face: Rect | None = None, beats: list[tuple] | None = None,
    sfx: list[tuple[float, int]] | None = None, sfx_gain_db: float = -4.0,
) -> str:  # fmt: skip
    """beats are on the output timeline (after the cold open); sfx = (output time, ffmpeg input index)."""
    g: list[str] = []
    co_d = cold_open[1] if cold_open else 0.0
    if cold_open:  # the reaction plays first in the same layout, cam punched in, then the clip
        cs, cd = cold_open
        g.append(
            "[0:v]split=2[cov][mnv];"
            f"[cov]trim=start={trim + cs:.3f}:duration={cd:.3f},setpts=PTS-STARTPTS[cov1];"
            f"[mnv]trim=start={trim:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS[mnv1];"
            "[cov1][mnv1]concat=n=2:v=1:a=0[sv]"
        )
    else:
        g.append(f"[0:v]trim=start={trim:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS[sv]")
    z = zoom_expr(co_d, zoom, [b[0] for b in beats or []])
    if has_webcam:
        centre = (face[0] + face[2] / 2, face[1] + face[3] / 2) if face else None
        g.append("[sv]split=2[cs][gs]")
        g.append(_zoomed("[cs]", fit_crop(cam, 1080 / 960, centre), (1080, 960), z, fps, "[cam]"))
        g.append(_zoomed("[gs]", fit_crop(game, 1080 / 960), (1080, 960), None, fps, "[game]"))
        g.append("[cam][game]vstack=inputs=2,format=yuv420p,setsar=1[v1]")
    else:
        fw, fh = src_wh
        g.append("[sv]split=2[bgs][fs]")
        g.append(
            "[bgs]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
            "scale=1080:1920,eq=brightness=-0.25:saturation=0.85,setsar=1[bg]"
        )
        g.append(_zoomed("[fs]", fit_crop((0, 0, fw, fh), 1080 / FRAME_H), (1080, FRAME_H), z, fps, "[full]"))
        g.append(f"[bg][full]overlay=0:{FRAME_Y},format=yuv420p,setsar=1[v1]")
    last = "[v1]"
    if logo_idx >= 0:
        g.append(f"[{logo_idx}:v]scale={SPLIT_LOGO_W}:-1,format=rgba[lg]")
        g.append(f"{last}[lg]overlay=(W-w)/2:{SPLIT_LOGO_Y}[v2]")
        last = "[v2]"
    for i, (t, k, *_) in enumerate(beats or []):
        if k in FX_FILTERS:
            d, f = FX_FILTERS[k]
            g.append(f"{last}{f}:enable='between(t,{t:.3f},{t + d:.3f})'[fx{i}]")
            last = f"[fx{i}]"
    g.append(
        f"{last}ass=filename={esc_filter_path(ass)}:fontsdir={esc_filter_path(fontsdir)},format=yuv420p[v]"
    )
    audio = _audio_chain(trim, dur, cold_open, measured)
    if sfx:
        audio = audio[: audio.rindex("[a]")] + "[am];" + _sfx_mix(sfx, sfx_gain_db)
    g.append(audio)
    return ";".join(g)


def render_short(
    src: Path, start_s: float, dur_s: float, out: Path, *, clip_id: str, streamer: str,
    words: list[Word], hook: str | None, platform: str = "youtube", style: str | None = None,
    accent: str | None = None, cam: Rect | None = None, game: Rect | None = None,
    has_webcam: bool = True, profile: str = "cpu_batch", captions: str | None = None,
    cold_open: tuple[float, float] | None = None, face: Rect | str | None = None,
    beats: list[tuple] | None = None, layout: str | None = None,
) -> Path:  # fmt: skip
    """cold_open=(start, duration) relative to start_s: that moment plays first, zoomed in, then the clip.
    The output is duration + dur_s long; captions follow the clip part. beats = judge beats (t relative to
    start_s, kind, label): zoom punch, colour effect and sound-effect label. layout split (default) | stack;
    split never draws the hook on the video (owner, 17 Nov)."""
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
    beats = [(b[0] + co_d, *b[1:]) for b in beats or [] if 0 <= b[0] <= dur_s]
    if (layout or layout_mode()) == "split":
        return _render_split(
            src, out, coarse=coarse, trim=trim, dur_s=dur_s, total=total, info=info, fps=fps, cam=cam,
            game=game, has_webcam=has_webcam, words=words, beats=beats, streamer=streamer,
            captions=captions or caption_style(clip_id), cold_open=cold_open, face=parse_rect(face),
            profile=profile, sfx=pick_sfx(beats),
        )  # fmt: skip

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
            game=game, gradient_idx=gradient_idx, logo_idx=logo_idx, hook_idx=hook_idx,
            ass=ass, fontsdir=FONTS_DIR, hook_s=hook_seconds(), cold_open=cold_open,
            zoom=co_settings()[1], measured=_measure(src, coarse, trim, dur_s, cold_open),
            src_wh=(info.width, info.height), fps=fps, face=parse_rect(face),
        )  # fmt: skip
        cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", *video_args(profile)]
        cmd += ["-fps_mode", "cfr", "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        cmd += ["-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
        _run(cmd)
    return out


def _render_split(
    src: Path, out: Path, *, coarse: float, trim: float, dur_s: float, total: float, info: SourceInfo,
    fps: float, cam: Rect, game: Rect, has_webcam: bool, words: list[Word], beats: list, streamer: str,
    captions: str, cold_open: tuple[float, float] | None, face: Rect | None, profile: str,
    sfx: list[tuple[float, Path]] | None = None,
) -> Path:  # fmt: skip
    cap_y = SEAM_Y if has_webcam else FRAME_CAP_Y
    credit_pos = (7, 24, SEAM_Y + 75) if has_webcam else (7, 24, FRAME_Y + FRAME_H + 12)
    use_logo = bool(config.global_config().get("render", {}).get("logo", True))
    with tempfile.TemporaryDirectory(prefix="clipwatcher_") as td:
        ass = write_ass(
            Path(td) / "subs.ass", words, total, credit=config.credit(streamer), style=captions,
            y=cap_y, beats=beats, credit_pos=credit_pos,
        )  # fmt: skip
        cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-ss", f"{coarse:.3f}", "-i", str(src)]
        if use_logo:
            cmd += ["-i", str(BRANDING_DIR / "top_logo.png")]
        first = 2 if use_logo else 1
        for _, f in sfx or []:
            cmd += ["-i", str(f)]
        sfx_in = [(t, first + j) for j, (t, _) in enumerate(sfx or [])]
        gain = float(config.global_config().get("render", {}).get("sfx_gain_db", -4.0))
        graph = build_split_graph(
            trim=trim, dur=dur_s, has_webcam=has_webcam, cam=cam, game=game, logo_idx=1 if use_logo else -1,
            ass=ass, fontsdir=FONTS_DIR, cold_open=cold_open, zoom=co_settings()[1],
            measured=_measure(src, coarse, trim, dur_s, cold_open), src_wh=(info.width, info.height),
            fps=fps, face=face, beats=beats, sfx=sfx_in, sfx_gain_db=gain,
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
