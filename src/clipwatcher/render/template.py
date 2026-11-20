"""One-pass ffmpeg render of the 9:16 template (D5/D4/D9) and the clean 16:9 export."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from clipwatcher import config
from clipwatcher.config import ffmpeg_bin, ffprobe_bin
from clipwatcher.models import Word
from clipwatcher.render import remotion as rm
from clipwatcher.render import seek
from clipwatcher.render.branding import ACCENTS, BRANDING_DIR, FONTS_DIR, ensure_branding, hook_pill
from clipwatcher.render.captions import caption_style, write_ass
from clipwatcher.render.cold_open import settings as co_settings
from clipwatcher.render.encoders import video_args

log = logging.getLogger(__name__)

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
    r"""A path as a filter option value inside -filter_complex.

    Two parsers read it. The filter's option parser treats \ ' : as special, the graph parser before it
    \ ' [ ] , ; so a colon needs \\: and an apostrophe \\\' to arrive as themselves. One level only
    (what this did before) left every ':' and "'" in a temp or fonts folder name breaking the render.
    Windows: forward slashes, and the drive colon "C:" becomes "C\\:" like any other colon."""
    if os.name == "nt":
        p = Path(p).as_posix()
    s = str(p)
    for special in ("\\':", "\\'[],;"):  # option level first, then graph level on top of it
        s = "".join("\\" + ch if ch in special else ch for ch in s)
    return s


def screen_credit(streamer: str) -> str | None:
    """The twitch.tv/<name> tag on the video, or None: off by default since 18 Nov (owner). The credit
    stays in the title/description metadata (config.credit)."""
    on = bool(config.global_config().get("render", {}).get("credit_on_screen", False))
    return config.credit(streamer) if on else None


def _seek(start_s: float) -> tuple[float, float]:
    coarse = max(0.0, start_s - 6)
    return coarse, start_s - coarse


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({r.returncode}): {r.stderr[-2000:]}")


LENGTH_TOL_S = 0.5  # frame and audio-packet rounding is a few hundredths of a second


def _verify(out: Path, want_s: float) -> None:
    """Raise RuntimeError unless out is the clip that was asked for. ffmpeg exits 0 when the source ends
    early: 6 s asked for 2 s before the end gave a 2 s file, a start past the end a file without streams,
    and both were staged as clips.

    The file must hold a picture and a sound stream, each want_s long. One case is let through with a
    warning: the recording ended before the clip did (the live lane cuts what exists when the buffer never
    fills, a VOD ends) and what is left still reaches the shortest clip allowed (render.clip_min_s, D14).
    A rejected file is removed so nothing can stage it."""
    from clipwatcher.judge.snap import clip_bounds

    try:
        r = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-show_entries", "stream=codec_type,duration:format=duration",
             "-of", "json", str(out)],
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        info = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        info = {}

    def seconds(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    whole = seconds(info.get("format", {}).get("duration"))
    got, problem = want_s, None
    for kind, name in (("video", "picture"), ("audio", "sound")):
        st = next((x for x in info.get("streams", []) if x.get("codec_type") == kind), None)
        if st is None:
            problem = f"no {kind} stream"
            break
        got = min(got, seconds(st.get("duration")) or whole)
        if got < min(want_s, clip_bounds()[0]) - LENGTH_TOL_S:
            problem = f"{got:.1f} s of {name} for a {want_s:.1f} s clip"
            break
    if problem:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"render failed, {problem} (does the recording end before the clip?): {out.name}")
    if got < want_s - LENGTH_TOL_S:
        log.warning("%s: %.1f s of %.1f s rendered, the recording ends there", out.name, got, want_s)


LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
# CFR: zoompan numbers its output frames 0, 1, 2 ... at the rate it is given and forgets their timestamps.
# Fed a picture with holes (a live recording whose encoder dropped frames) it closes them: after half a
# second of dropped frames the zoomed webcam ran half a second ahead of the sound and of the gameplay.
# So every picture that can reach a zoom is first put on the constant output frame grid (fps filter,
# which fills a hole by repeating the frame before it), where frame number and time are the same thing.


def _segments(
    src: str, trim: float, keep: list[tuple[float, float]], out: str, *, audio: bool, fps: float = 30.0
) -> str:
    """src -> out: the kept stretches of the clip (jump cuts), back to back. 15 ms fades on audio cuts.

    The picture is always put on the constant frame grid of fps first (see CFR). With cuts, keep lies on
    that grid (jumpcut.on_frames): the picture is cut by frame number, the sound at the same instants, so
    both pieces are equally long."""
    n = len(keep)
    tr, sp = ("atrim", "asetpts") if audio else ("trim", "setpts")
    if n == 1:
        a, b = keep[0]
        grid = "" if audio else f",fps={fps:g}"
        return f"{src}{tr}=start={trim + a:.3f}:duration={b - a:.3f},{sp}=PTS-STARTPTS{grid}{out}"
    tag = "ja" if audio else "jv"
    outs = "".join(f"[{tag}{i}]" for i in range(n))
    if audio:
        g = [f"{src}asplit={n}{outs}"]
    else:  # constant frame rate from the clip start: frame k is k / fps into the clip
        end = trim + keep[-1][1] + 1.0
        g = [f"{src}trim=start={trim:.3f}:end={end:.3f},setpts=PTS-STARTPTS,fps={fps:g},split={n}{outs}"]
    for i, (a, b) in enumerate(keep):
        d = b - a
        if audio:
            cut = f"atrim=start={trim + a:.6f}:duration={d:.6f}"
            fade = f",afade=t=in:d=0.015,afade=t=out:st={max(0.0, d - 0.015):.3f}:d=0.015"
        else:
            cut, fade = f"trim=start_frame={round(a * fps)}:end_frame={round(b * fps)}", ""
        g.append(f"[{tag}{i}]{cut},{sp}=PTS-STARTPTS{fade}[{tag}{i}o]")
    g.append(
        "".join(f"[{tag}{i}o]" for i in range(n))
        + f"concat=n={n}:v={0 if audio else 1}:a={1 if audio else 0}{out}"
    )
    return ";".join(g)


def _audio_chain(
    trim: float, dur: float, cold_open: tuple[float, float] | None = None, measured: dict | None = None,
    keep: list[tuple[float, float]] | None = None,
) -> str:  # fmt: skip
    """Audio graph ending in [a]. measured = loudnorm pass-1 stats: one fixed gain + limiter instead of
    loudnorm's dynamic mode, which rides the gain on the first seconds (a loud cold open came out garbled).
    keep = jump-cut stretches of the clip (relative to its start); None = the whole clip."""
    if measured:  # fixed gain to -14 LUFS + true-peak-ish limiter at -1.5 dBFS (0.841)
        gain = max(-30.0, min(30.0, -14.0 - float(measured["input_i"])))
        norm = f"volume={gain:.2f}dB,alimiter=limit=0.841:level=false:latency=true"
    else:
        norm = LOUDNORM
    norm += ",aresample=48000[a]"
    if keep and len(keep) > 1:
        if not cold_open:
            return _segments("[0:a]", trim, keep, "[mnj]", audio=True) + f";[mnj]{norm}"
        cs, cd = cold_open
        return (
            "[0:a]asplit=2[coa][mna];"
            f"[coa]atrim=start={trim + cs:.3f}:duration={cd:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={cd - 0.06:.3f}:d=0.06[coa1];"
            + _segments("[mna]", trim, keep, "[mna1]", audio=True)
            + f";[coa1][mna1]concat=n=2:v=0:a=1,{norm}"
        )
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


def _measure(src: Path, coarse: float, trim: float, dur: float, cold_open=None, keep=None) -> dict | None:
    """loudnorm pass 1 over the exact audio the render will use; None on failure (dynamic mode then).
    It decodes the sound alone, so it seeks like every single-stream read (render/seek.py)."""
    inp, at = seek.one_stream(src, coarse + trim)
    chain = _audio_chain(at, dur, cold_open, keep=keep).replace(
        f"{LOUDNORM},aresample=48000[a]", f"{LOUDNORM}:print_format=json[a]"
    )
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", *inp]
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
    face: Rect | None = None, overlay: tuple[int, rm.Overlay] | None = None, probe: str | None = None,
) -> str:  # fmt: skip
    """overlay = (ffmpeg input index, Remotion caption overlay) laid over the picture after the ASS pass;
    probe = a filter chain that replaces the captions and the sound: the graph then ends in the picture
    under the captions, sampled for the auto colours."""
    g: list[str] = []
    use_bg = style == "ambient"
    grad = f"[{gradient_idx}:v]"
    if cold_open:  # webcam-only replay of the reaction, then the clip from its start
        cs, cd = cold_open
        g.append(
            "[0:v]split=2[cov][mnv];"
            f"[cov]trim=start={trim + cs:.3f}:duration={cd:.3f},setpts=PTS-STARTPTS,fps={fps:g}[cov1];"  # CFR
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
        centre = (face[0] + face[2] / 2, face[1] + face[3] / 2) if face else None
        cx, cy, cw, ch = fit_crop(cam, 1080 / 480, centre)  # the panel's shape: never stretched
        gx, gy, gw, gh = fit_crop(game, 1080 / 540)
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
    if probe:
        return ";".join([*g, f"[v4]{probe}[v]"])
    g += _caption_tail("[v4]", ass, fontsdir, overlay)
    g.append(_audio_chain(trim, dur, cold_open, measured))
    return ";".join(g)


def _caption_tail(last: str, ass: Path, fontsdir: Path, overlay: tuple[int, rm.Overlay] | None) -> list[str]:
    """last -> [v]: the ASS pass (captions, or only the credit), then the Remotion overlay if there is one."""
    burn = f"{last}ass=filename={esc_filter_path(ass)}:fontsdir={esc_filter_path(fontsdir)},format=yuv420p"
    if not overlay:
        return [burn + "[v]"]
    return [burn + "[capbase]", rm.overlay_filter(overlay[0], overlay[1], "[capbase]", "[v]")]


# -- split layout (owner, 17 Nov evening): full-bleed webcam over gameplay, 1080x960 each, captions on the
# seam, no on-screen hook. No webcam -> the full 16:9 frame over a blurred copy, captions under it.
LAYOUTS = ("split", "stack")
SEAM_Y = 960
FRAME_H = 760  # no-webcam frame 1080x760 (16:9 source, sides trimmed a little) -> y580-1340
FRAME_Y = 580
FRAME_CAP_Y = 1450
REFRAME_CAP_Y = 1250  # full-screen reframe: captions under the face, above the Shorts UI
SPLIT_LOGO_W, SPLIT_LOGO_Y = 520, 40  # CapybarasKing red banner (owner, 19 Nov), 520x87
PUNCH, PUNCH_S = 0.12, 0.45  # zoom punch on a beat: +12 %, eased out over 0.45 s
FX_FILTERS = {  # beat kind -> (seconds, filter) applied to the whole picture under the captions
    "fail": (0.7, "colorchannelmixer=rr=1:rg=0.35:rb=0.35:gr=0:gg=0.4:gb=0:br=0:bg=0:bb=0.4"),
    "shock": (1.6, "hue=s=0"),
    "hype": (0.3, "colorchannelmixer=rr=0.55:gr=0.3:gg=1.25:gb=0.3:bb=0.55"),
}


SFX_LIMIT = 0.891  # -1 dBFS ceiling after the sound effects are mixed in


CUE_GAP_S = 0.35  # a pause at least this long between words can take a label or a sound
CUE_SHIFT_S = 1.5  # ...if it starts within this many seconds after the beat


def cue_time(t: float, words: list[Word]) -> float | None:
    """When a beat's label and sound can play without covering speech: t itself when nobody talks there,
    else the first pause after the line in progress (the sound lands after the punchline, as clip
    channels do), None when no pause comes within CUE_SHIFT_S."""
    ws = sorted((w for w in words if w.text.strip()), key=lambda w: w.start)
    if not any(w.start - 0.05 < t + 0.4 and t - 0.05 < w.end for w in ws):
        return t
    for i, w in enumerate(ws):
        if w.end < t - 0.05:
            continue
        nxt = ws[i + 1].start if i + 1 < len(ws) else None
        if nxt is None or nxt - w.end >= CUE_GAP_S:
            c = round(w.end + 0.05, 3)
            return c if c - t <= CUE_SHIFT_S else None
    return None


def cues(beats: list[tuple], words: list[Word]) -> list[tuple]:
    """Beats for the label and the sound: moved off speech, or stripped of both when no pause is near.
    The zoom and colour effect stay on the beat itself (the event)."""
    out = []
    for b in beats:
        lb, sx = (b[2], b[3] if len(b) > 3 else "")
        if not lb and not sx:
            continue
        c = cue_time(b[0], words)
        if c is not None:
            out.append((c, b[1], lb, sx))
    return out


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


def split_crops(cam: Rect, game: Rect, face: Rect | None, src_wh: tuple[int, int]) -> tuple[Rect, Rect]:
    """Source crops of the split layout's two 1080x960 panels: the largest crop of the panel's shape inside
    the webcam rectangle, placed on the face (or on where it went: layout passes the span), and inside the
    gameplay rectangle. Both stay inside the source picture."""
    fw, fh = src_wh
    centre = (face[0] + face[2] / 2, face[1] + face[3] / 2) if face else None
    return fit_crop(_inside(cam, fw, fh), 1080 / 960, centre), fit_crop(_inside(game, fw, fh), 1080 / 960)


def _inside(r: Rect, fw: int, fh: int) -> Rect:
    x, y = min(max(r[0], 0), fw - 2), min(max(r[1], 0), fh - 2)
    return x, y, max(2, min(r[2], fw - x)), max(2, min(r[3], fh - y))


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
    keep: list[tuple[float, float]] | None = None, reframe_x: str | None = None,
    overlay: tuple[int, rm.Overlay] | None = None, probe: str | None = None,
) -> str:  # fmt: skip
    """beats are on the output timeline (after the cold open); sfx = (output time, ffmpeg input index);
    keep = jump-cut stretches of the clip (relative to its start), None = the whole clip.
    overlay / probe: as in build_short_graph."""
    g: list[str] = []
    co_d = cold_open[1] if cold_open else 0.0
    keep = keep or [(0.0, dur)]
    if cold_open:  # the reaction plays first in the same layout, cam punched in, then the clip
        cs, cd = cold_open
        g.append(
            "[0:v]split=2[cov][mnv];"
            f"[cov]trim=start={trim + cs:.3f}:duration={cd:.3f},setpts=PTS-STARTPTS,fps={fps:g}[cov1];"  # CFR
            + _segments("[mnv]", trim, keep, "[mnv1]", audio=False, fps=fps)
            + ";[cov1][mnv1]concat=n=2:v=1:a=0[sv]"
        )
    else:
        g.append(_segments("[0:v]", trim, keep, "[sv]", audio=False, fps=fps))
    z = zoom_expr(co_d, zoom, [b[0] for b in beats or []])
    if has_webcam:
        cam_crop, game_crop = split_crops(cam, game, face, src_wh)
        g.append("[sv]split=2[cs][gs]")
        g.append(_zoomed("[cs]", cam_crop, (1080, 960), z, fps, "[cam]"))
        g.append(_zoomed("[gs]", game_crop, (1080, 960), None, fps, "[game]"))
        g.append("[cam][game]vstack=inputs=2,format=yuv420p,setsar=1[v1]")
    elif reframe_x:  # full-screen 9:16 window following the person (crop x is evaluated per frame)
        fw, fh = src_wh
        cw = int(fh * 9 / 16) // 2 * 2
        g.append(f"[sv]crop={cw}:{fh}:'{reframe_x}':0,scale=1080:1920:flags=lanczos,setsar=1[rf]")
        if z:  # punches on the whole picture, 1x canvas (2x of a full 1080x1920 frame is too slow)
            g.append(
                f"[rf]zoompan=z='{z}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=1080x1920:fps={fps:g},"
                "setpts=PTS-STARTPTS,format=yuv420p,setsar=1[v1]"
            )
        else:
            g.append("[rf]format=yuv420p[v1]")
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
    if probe:
        return ";".join([*g, f"{last}{probe}[v]"])
    g += _caption_tail(last, ass, fontsdir, overlay)
    audio = _audio_chain(trim, dur, cold_open, measured, keep=keep)
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
    actions: list[tuple[float, float, str]] | None = None, reframe: bool = False,
    report: dict | None = None,
) -> Path:  # fmt: skip
    """cold_open=(start, duration) relative to start_s: that moment plays first, zoomed in, then the clip.
    The output is duration + dur_s long; captions follow the clip part. beats = judge beats (t relative to
    start_s, kind, label): zoom punch, colour effect and sound-effect label. layout split (default) | stack;
    split never draws the hook on the video (owner, 17 Nov). report, when given, is filled with what the
    Short got: `captions` (the style on the video: the ASS fallback when an rm: template could not be
    rendered) and then `captions_error` (why)."""
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
    fps = float(f"{min(info.fps, 60.0):g}")  # as ffmpeg is told it (-r, fps=): frame maths uses the same
    co_d = cold_open[1] if cold_open else 0.0
    split = (layout or layout_mode()) == "split"
    keep = [(0.0, dur_s)]
    uncut = list(words)  # the cold open plays uncut source time: its captions come from these
    if split:  # jump cuts: dead air between lines goes (beats and action labels are protected)
        from clipwatcher.render import jumpcut

        protect = [(b[0] - jumpcut.PROTECT_S, b[0] + jumpcut.PROTECT_S) for b in beats or []]
        protect += [(a[0], a[1]) for a in actions or []]
        keep = jumpcut.on_frames(jumpcut.keep_ranges(words, dur_s, protect=protect), fps)
        if len(keep) > 1:
            words = jumpcut.remap_words(words, keep)
            beats = [(t, *b[1:]) for b in beats or [] if (t := jumpcut.remap(b[0], keep)) is not None]
            actions = [
                (s, e, *a[2:])
                for a in actions or []
                if (s := jumpcut.remap(a[0], keep)) is not None
                and (e := jumpcut.remap(a[1], keep)) is not None
            ]
    main_d = sum(b - a for a, b in keep)
    total = main_d + co_d
    if cold_open:  # captions for the replayed shout (sound-off viewers), then the clip re-timed after it
        cs = cold_open[0]
        co_words = [
            Word(max(0.0, w.start - cs), min(co_d, w.end - cs), w.text)
            for w in uncut
            if cs - 0.05 <= w.start < cs + co_d - 0.1
        ]
        words = co_words + [Word(w.start + co_d, w.end + co_d, w.text) for w in words]
    beats = [(b[0] + co_d, *b[1:]) for b in beats or [] if 0 <= b[0] <= main_d]
    actions = [(a[0] + co_d, min(a[1], main_d) + co_d, *a[2:]) for a in actions or [] if a[0] < main_d]
    do_reframe = reframe and not has_webcam
    if split:
        return _render_split(
            src, out, coarse=coarse, trim=trim, dur_s=dur_s, total=total, info=info, fps=fps, cam=cam,
            game=game, has_webcam=has_webcam, words=words, beats=beats, streamer=streamer,
            captions=captions or caption_style(clip_id), cold_open=cold_open, face=parse_rect(face),
            profile=profile, sfx=pick_sfx(cues(beats, words)), keep=keep, actions=actions,
            cue_beats=cues(beats, words), report=report,
            reframe_x=_reframe_path(src, start_s, dur_s, info, keep, cold_open) if do_reframe else None,
        )  # fmt: skip

    with tempfile.TemporaryDirectory(prefix="clipwatcher_") as td:
        work = Path(td)
        cap_style = captions or caption_style(clip_id)
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
        gk = dict(
            trim=trim, dur=dur_s, style=style, has_webcam=has_webcam, has_hook=bool(hook), cam=cam,
            game=game, gradient_idx=gradient_idx, logo_idx=logo_idx, hook_idx=hook_idx,
            fontsdir=FONTS_DIR, hook_s=hook_seconds(), cold_open=cold_open,
            zoom=co_settings()[1], measured=_measure(src, coarse, trim, dur_s, cold_open),
            src_wh=(info.width, info.height), fps=fps, face=parse_rect(face),
        )  # fmt: skip
        tail = [*video_args(profile), "-fps_mode", "cfr", "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "192k"]
        tail += ["-ar", "48000", "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]

        def encode(style_: str, overlay: rm.Overlay | None) -> None:
            shown = [] if overlay or rm.is_rm(style_) else words  # with an overlay the ASS file is the credit
            ass = write_ass(
                work / "subs.ass", shown, total, credit=screen_credit(streamer),
                style="word" if rm.is_rm(style_) else style_,
            )  # fmt: skip
            extra = rm.overlay_input(overlay) if overlay else []
            graph = build_short_graph(**gk, ass=ass, overlay=(idx, overlay) if overlay else None)
            _run([*cmd, *extra, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", *tail])

        used = _encode_with_captions(
            encode, cap_style, work, words, total, fps, rm.caption_box(1495, "stack"), None, None,
            lambda p: [*cmd, "-filter_complex", build_short_graph(**gk, ass=work / "none.ass", probe=p)],
        )  # fmt: skip
        if report is not None:
            report.update(used)
    _verify(out, total)
    return out


def _probe_background(cmd: list[str], total: float) -> rm.Background | None:
    """The picture under the captions, sampled small and slow, for the auto colours. cmd is the render
    command with a probe graph; None when it cannot be read (the fixed colours are used then)."""
    try:
        r = subprocess.run(
            [*cmd, "-map", "[v]", "-t", f"{total:.3f}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=600,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("caption colours: could not sample the picture (%s), using the fixed colours", e)
        return None
    if r.returncode != 0 or not r.stdout:
        log.warning("caption colours: sampling failed, using the fixed colours: %s", r.stderr[-300:])
        return None
    return rm.background_from_raw(r.stdout)


def _encode_with_captions(
    encode, style: str, work: Path, words: list[Word], total: float, fps: float, box: rm.Box,
    beats: list | None, actions: list | None, probe_cmd,
) -> dict:  # fmt: skip
    """Run encode(style, overlay) with the captions the style asks for. For rm:<template> the overlay is
    rendered first; if Node, the bundle or the render is missing or fails, or ffmpeg cannot use the result,
    the clip is encoded with the ASS fallback style instead: captions never cost a clip. Returns what went
    on the video: {"captions": style} and, after a fallback, "captions_error"."""
    if not rm.is_rm(style):
        encode(style, None)
        return {"captions": style}
    fb = rm.fallback_style()
    try:
        bg = None
        if rm.color_mode() == "auto":
            bg = _probe_background(probe_cmd(rm.probe_filter(box)), total)
        overlay = rm.render_overlay(
            words, total, rm.template_name(style), work / "rm", box=box, fps=fps, beats=beats,
            actions=actions, background=bg,
        )  # fmt: skip
    except Exception as e:  # noqa: BLE001 - any failure here means ASS captions, never a lost clip
        level = log.warning if isinstance(e, rm.RemotionError) else log.exception
        level("Remotion captions %s not rendered (%s): using the ASS style %s", style, e, fb)
        encode(fb, None)
        return {"captions": fb, "captions_error": str(e)[-400:]}
    try:
        encode(style, overlay)
    except RuntimeError as e:
        if overlay is None:
            raise
        log.warning("render with the Remotion overlay failed (%s): using the ASS style %s", str(e)[-300:], fb)
        encode(fb, None)
        return {"captions": fb, "captions_error": str(e)[-400:]}
    return {"captions": style}


def _reframe_path(
    src: Path, start_s: float, dur_s: float, info: SourceInfo, keep: list[tuple[float, float]],
    cold_open: tuple[float, float] | None,
) -> str | None:  # fmt: skip
    """crop x(t) expression on the output timeline that follows the main face, or None (boxed frame)."""
    from clipwatcher.render import jumpcut, reframe

    if not reframe.enabled():
        return None
    samples = reframe.track(src, start_s, dur_s)
    if reframe.coverage(samples) < reframe.MIN_COVER:
        return None
    path = reframe.smooth(samples)
    co_d = cold_open[1] if cold_open else 0.0
    pts = []
    for t, x in path:
        if cold_open and cold_open[0] <= t <= cold_open[0] + co_d:
            pts.append((round(t - cold_open[0], 2), x))
        m = jumpcut.remap(t, keep) if keep else t
        if m is not None:
            pts.append((round(m + co_d, 2), x))
    crop_w = int(info.height * 9 / 16) // 2 * 2
    return reframe.x_expr(reframe.keyframes(sorted(pts)), info.width, crop_w)


def _render_split(
    src: Path, out: Path, *, coarse: float, trim: float, dur_s: float, total: float, info: SourceInfo,
    fps: float, cam: Rect, game: Rect, has_webcam: bool, words: list[Word], beats: list, streamer: str,
    captions: str, cold_open: tuple[float, float] | None, face: Rect | None, profile: str,
    sfx: list[tuple[float, Path]] | None = None, keep: list[tuple[float, float]] | None = None,
    actions: list[tuple[float, float, str]] | None = None, cue_beats: list[tuple] | None = None,
    reframe_x: str | None = None, report: dict | None = None,
) -> Path:  # fmt: skip
    """beats drive zoom and colour effects; cue_beats (labels and sounds moved off speech) the captions.
    reframe_x: crop x(t) of a full-screen 9:16 window that follows the person (no-webcam clips)."""
    cap_y = SEAM_Y if has_webcam else (REFRAME_CAP_Y if reframe_x else FRAME_CAP_Y)
    credit_pos = (
        (7, 24, SEAM_Y + 75) if has_webcam else (7, 24, 170) if reframe_x else (7, 24, FRAME_Y + FRAME_H + 12)
    )
    use_logo = bool(config.global_config().get("render", {}).get("logo", True))
    label_beats = beats if cue_beats is None else cue_beats
    with tempfile.TemporaryDirectory(prefix="clipwatcher_") as td:
        cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-ss", f"{coarse:.3f}", "-i", str(src)]
        if use_logo:
            cmd += ["-i", str(BRANDING_DIR / "top_logo.png")]
        first = 2 if use_logo else 1
        for _, f in sfx or []:
            cmd += ["-i", str(f)]
        sfx_in = [(t, first + j) for j, (t, _) in enumerate(sfx or [])]
        gain = float(config.global_config().get("render", {}).get("sfx_gain_db", -4.0))
        gk = dict(
            trim=trim, dur=dur_s, has_webcam=has_webcam, cam=cam, game=game, logo_idx=1 if use_logo else -1,
            fontsdir=FONTS_DIR, cold_open=cold_open, zoom=co_settings()[1],
            measured=_measure(src, coarse, trim, dur_s, cold_open, keep), src_wh=(info.width, info.height),
            fps=fps, face=face, beats=beats, sfx=sfx_in, sfx_gain_db=gain, keep=keep, reframe_x=reframe_x,
        )  # fmt: skip
        tail = [*video_args(profile), "-fps_mode", "cfr", "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "192k"]
        tail += ["-ar", "48000", "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
        n_inputs = first + len(sfx or [])

        def encode(style: str, overlay: rm.Overlay | None) -> None:
            blank = bool(overlay) or rm.is_rm(
                style
            )  # the overlay carries the words: the ASS file is the credit
            ass = write_ass(
                Path(td) / "subs.ass", [] if blank else words, total, credit=screen_credit(streamer),
                style="word" if blank else style, y=cap_y, beats=None if blank else label_beats,
                credit_pos=credit_pos, actions=None if blank else actions,
            )  # fmt: skip
            extra = rm.overlay_input(overlay) if overlay else []
            graph = build_split_graph(**gk, ass=ass, overlay=(n_inputs, overlay) if overlay else None)
            _run([*cmd, *extra, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", *tail])

        kind = "seam" if has_webcam else "reframe" if reframe_x else "frame"
        used = _encode_with_captions(
            encode, captions, Path(td), words, total, fps, rm.caption_box(cap_y, kind), label_beats, actions,
            lambda p: [*cmd, "-filter_complex", build_split_graph(**gk, ass=Path(td) / "no.ass", probe=p)],
        )  # fmt: skip
        if report is not None:
            report.update(used)
    _verify(out, total)
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
    _verify(out, dur_s)
    return out
