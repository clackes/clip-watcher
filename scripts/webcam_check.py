#!/usr/bin/env python3
"""Webcam check: is the streamer's webcam found and shown well in the 9:16 Short, on real recordings?

The check compares the code with what a person sees. A folder of samples is recorded with
scripts/webcam_record.py and labelled by eye (labels/<login>.json: is there a webcam, its true rectangle
in source pixels, when it changes). This script then runs the real path on every sample:

  detect   layout.resolve as the live lane calls it, at several moments of each sample
  render   the Short of each sample through that layout decision (render_short, live profile)
  frames   every frame of every Short against the source: panels show the crop they should, the face is
           there and whole wherever the source shows it, nothing black or unfilled, lengths as asked
  sharp    what the cam panel loses against a reference upscale of the same source crop
  score    detect / frames results against the labels
  gallery  per sample: source frame with the labelled and the detected rectangles, the Short beside it

.venv/bin/python scripts/webcam_check.py <step> ROOT [logins ...] [--set NAME] [--jobs 6]
ROOT holds rec/<login>.ts, gt/labels/<login>.json and gets det/, render/, frames/, gallery/ per --set.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

MOMENTS = (14.0, 28.0, 42.0, 56.0, 70.0)  # peak times (s) each sample is resolved at
CLIP_T0, CLIP_DUR, CLIP_PEAK = 22.0, 30.0, 44.0  # the Short that is rendered and checked frame by frame
PANEL_W, PANEL_H = 1080, 960
SPILL_PX = 4  # the shown cam crop may reach this far past the true cam rectangle (source px)
ZOOM_MAX = 1.25  # ... and may be this much tighter than the largest crop the true cam allows
LOOSE_IOU = 0.3  # cut-out / round cams and avatars: overlap of the shown crop with the person's box
PSNR_MIN = 26.0  # panel against the source crop it should show, compared at quarter size
SHARP_EVERY = 100  # sharpness is measured on every 100th frame of a Short
REFRAME_JUMP = 24  # source px the full-screen window may move from one frame to the next
VERBOSE = False  # score: also the per-moment failure kinds of the first check
OWNER = ("zackrawrr", "ishowspeed", "clix", "tfue", "kaicenat", "ninja", "myth", "harry", "uhsnow", "matteu")


# --------------------------------------------------------------------------- helpers
def samples(root: Path, logins: list[str]) -> list[str]:
    have = sorted(p.stem for p in (root / "rec").glob("*.ts"))
    return [s for s in have if not logins or s in logins]


def shown_crops(lay: dict, fw: int, fh: int) -> dict:
    """What the split layout puts on screen for a layout decision: source rects (x, y, w, h)."""
    from clipwatcher.render import template as tpl

    out: dict = {"mode": "frame"}
    if lay["has_webcam"]:
        info = tpl.SourceInfo(fw, fh, 60.0)
        cam = tpl.parse_rect(lay["cam"]) or tpl.default_cam(info)
        game = tpl.parse_rect(lay["game"]) or tpl.default_game(info)
        face = tpl.parse_rect(lay.get("face"))
        if hasattr(tpl, "split_crops"):
            c, g = tpl.split_crops(cam, game, face, (fw, fh))
        else:
            centre = (face[0] + face[2] / 2, face[1] + face[3] / 2) if face else None
            c, g = tpl.fit_crop(cam, PANEL_W / PANEL_H, centre), tpl.fit_crop(game, PANEL_W / PANEL_H)
        out.update(mode="split", cam=list(c), game=list(g))
    elif lay.get("reframe"):
        out.update(mode="reframe")
    else:
        out.update(frame=list(tpl.fit_crop((0, 0, fw, fh), PANEL_W / tpl.FRAME_H)))
    return out


def probe_wh(media: Path) -> tuple[int, int, float]:
    from clipwatcher.render import template as tpl

    i = tpl.probe(media)
    return i.width, i.height, float(f"{min(i.fps, 60.0):g}")


def lay_dict(lay) -> dict:
    d = {
        "has_webcam": lay.has_webcam,
        "cam": lay.cam,
        "game": lay.game,
        "source": lay.source,
        "face": lay.face,
    }
    d["reframe"] = bool(lay.as_render_kwargs().get("reframe"))
    for k in ("sure", "doubts", "evidence"):  # how sure the decision is and the numbers behind it
        if hasattr(lay, k):
            d[k] = getattr(lay, k)
    return d


# --------------------------------------------------------------------------- detect
def _detect_one(args: tuple[str, str, str]) -> dict:
    root_s, login, backend = args
    from clipwatcher import calibrate, layout

    root = Path(root_s)
    media = root / "rec" / f"{login}.ts"
    fw, fh, _ = probe_wh(media)
    det = (
        calibrate.make_detector("/nonexistent/model.tflite")
        if backend == "haar"
        else calibrate.make_detector()
    )
    dur = calibrate.media_duration(media)
    rows = []
    for peak in MOMENTS:
        if peak + 2.5 > dur:
            continue
        t = time.time()
        try:
            lay = lay_dict(layout.resolve(media, login, peak_s=peak, cam_hint=None, detector=det))
            lay["shown"] = shown_crops(lay, fw, fh)
        except Exception as e:  # noqa: BLE001
            lay = {"error": repr(e)}
        lay.update(peak=peak, secs=round(time.time() - t, 1))
        rows.append(lay)
    return {"login": login, "backend": backend, "wh": [fw, fh], "moments": rows}


def cmd_detect(root: Path, logins: list[str], tag: str, jobs: int, backends: list[str]) -> None:
    out = root / "det" / tag
    out.mkdir(parents=True, exist_ok=True)
    work = [(str(root), s, b) for s in samples(root, logins) for b in backends]
    with ProcessPoolExecutor(jobs) as ex:
        for r in ex.map(_detect_one, work):
            (out / f"{r['login']}.{r['backend']}.json").write_text(json.dumps(r, indent=1))
            print(
                r["login"], r["backend"], [m.get("source", m.get("error")) for m in r["moments"]], flush=True
            )


# --------------------------------------------------------------------------- render
def _render_one(args: tuple[str, str, str]) -> dict:
    root_s, login, tag = args
    from clipwatcher import layout
    from clipwatcher.render.template import render_short

    root = Path(root_s)
    media = root / "rec" / f"{login}.ts"
    out = root / "render" / tag / f"{login}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    fw, fh, fps = probe_wh(media)
    res: dict = {"login": login, "wh": [fw, fh], "fps": fps, "t0": CLIP_T0, "dur": CLIP_DUR}
    t = time.time()
    try:
        lay = layout.resolve(media, login, peak_s=CLIP_PEAK, cam_hint=None)
        res["layout"] = lay_dict(lay)
        res["layout"]["shown"] = shown_crops(res["layout"], fw, fh)
        res["layout_s"] = round(time.time() - t, 1)
        t = time.time()
        render_short(
            media, CLIP_T0, CLIP_DUR, out, clip_id=f"wc-{login}", streamer=login, words=[], hook=None,
            **lay.as_render_kwargs(), profile="cpu_live", cold_open=None, beats=[], actions=[],
        )  # fmt: skip
        res["render_s"] = round(time.time() - t, 1)
        res["ok"] = True
    except Exception as e:  # noqa: BLE001
        res.update(ok=False, error=repr(e)[:600])
    (out.with_suffix(".json")).write_text(json.dumps(res, indent=1))
    return res


def cmd_render(root: Path, logins: list[str], tag: str, jobs: int) -> None:
    work = [(str(root), s, tag) for s in samples(root, logins)]
    with ProcessPoolExecutor(jobs) as ex:
        for r in ex.map(_render_one, work):
            lay = r.get("layout", {})
            print(
                r["login"], r.get("ok"), lay.get("source"), lay.get("shown"), r.get("error", ""), flush=True
            )


# --------------------------------------------------------------------------- every frame
def _pipe(cmd: list[str], w: int, h: int, ch: int = 1):
    import numpy as np

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=w * h * ch * 4)
    n = w * h * ch
    try:
        while True:
            buf = p.stdout.read(n)  # type: ignore[union-attr]
            if len(buf) < n:
                break
            yield np.frombuffer(buf, np.uint8).reshape((h, w) if ch == 1 else (h, w, ch))
    finally:
        p.kill()
        p.wait()


def source_frames(media: Path, start: float, dur: float, fps: float, w: int, h: int, pix: str = "gray"):
    """The frames the render reads: same seek, trim and frame grid."""
    from clipwatcher import config
    from clipwatcher.render import seek

    inp, trim = seek.one_stream(media, start)
    vf = f"trim=start={trim:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS,fps={fps:g}"
    cmd = [config.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", *inp, "-an", "-vf", vf, "-pix_fmt", pix,
           "-f", "rawvideo", "-"]  # fmt: skip
    return _pipe(cmd, w, h, 1 if pix == "gray" else 3)


def short_frames(mp4: Path, pix: str = "gray"):
    from clipwatcher import config

    cmd = [config.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(mp4), "-an", "-pix_fmt", pix,
           "-f", "rawvideo", "-"]  # fmt: skip
    return _pipe(cmd, 1080, 1920, 1 if pix == "gray" else 3)


def psnr(a, b, mask=None) -> float:
    import numpy as np

    d = (a.astype(np.float32) - b.astype(np.float32)) ** 2
    mse = float(d[mask].mean()) if mask is not None else float(d.mean())
    return 99.0 if mse < 1e-6 else 10 * math.log10(255.0**2 / mse)


def _durations(mp4: Path) -> dict:
    from clipwatcher import config

    r = subprocess.run(
        [config.ffprobe_bin(), "-v", "error", "-show_entries",
         "stream=codec_type,duration,nb_frames,width,height", "-of", "json", str(mp4)],
        capture_output=True, text=True,
    )  # fmt: skip
    out = {}
    for s in json.loads(r.stdout or "{}").get("streams", []):
        out[s["codec_type"]] = {k: s.get(k) for k in ("duration", "nb_frames", "width", "height")}
    return out


def _face_boxes(det_run, rgb) -> list[tuple[float, float, float, float]]:
    return det_run(rgb)


def _single_pass_detector():
    """One full-image pass of the face model (the panel is one big face, no tiling needed)."""
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    from clipwatcher import calibrate

    opts = vision.FaceDetectorOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(calibrate.DEFAULT_FACE_MODEL)),
        min_detection_confidence=0.5,
    )
    det = vision.FaceDetector.create_from_options(opts)

    def run(rgb):
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        return [
            (b.origin_x, b.origin_y, b.width, b.height)
            for b in (d.bounding_box for d in det.detect(img).detections)
        ]

    return run


LOGO_BOX = (270, 30, 810, 190)  # x0, y0, x1, y1 of the logo on the Short (left out of comparisons)
CREDIT_BOX = (0, 1010, 520, 1080)


def _stream(inputs: list[str], graph: str, w: int, h: int):
    """Frames (h, w, 3) of one ffmpeg filter graph ending in [o]."""
    from clipwatcher import config

    cmd = [config.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", *inputs, "-an", "-filter_complex",
           graph, "-map", "[o]", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]  # fmt: skip
    return _pipe(cmd, w, h, 3)


def _rendered_mode(mp4: Path, shown: dict) -> str:
    """split | frame | reframe as the Short was really rendered: a layout asks for a reframe, the renderer
    boxes the frame when it finds no face to follow. A boxed Short has a blurred backdrop above the frame."""
    import cv2

    if shown["mode"] != "reframe":
        return shown["mode"]
    for f in _stream(["-ss", "2", "-i", str(mp4)], "[0:v]scale=540:960[o]", 540, 960):
        g = cv2.cvtColor(f, cv2.COLOR_RGB2GRAY)
        return "frame" if detail(g[60:260]) < 0.15 * max(detail(g[330:630]), 1e-6) else "reframe"
    return "reframe"


def _frames_one(args: tuple[str, str, str]) -> dict:
    """Every frame of one Short against the source frame it was made from.

    split: each panel against the source crop it should show (PSNR at quarter size), black strips at a
    panel's edges, the face in the cam panel, and the face in the labelled cam of the source frame: a face
    whole in the true cam must be whole in the crop that is shown. frame: the boxed picture against the
    source. reframe: where the 9:16 window is in the source, frame by frame, and the face in it."""
    root_s, login, tag = args
    import cv2
    import numpy as np

    from clipwatcher.render import seek
    from clipwatcher.render import template as tpl

    root = Path(root_s)
    media = root / "rec" / f"{login}.ts"
    mp4 = root / "render" / tag / f"{login}.mp4"
    meta = json.loads(mp4.with_suffix(".json").read_text())
    res: dict = {"login": login, "ok": False}
    if not meta.get("ok"):
        res["error"] = "render failed: " + meta.get("error", "")
        return res
    fw, fh = meta["wh"]
    fps = meta["fps"]
    shown = meta["layout"]["shown"]
    mode = _rendered_mode(mp4, shown)
    gt = load_label(root, login)
    gt_cam = cam_at(gt, root, login, CLIP_PEAK) if gt else None
    res.update(mode=mode, durations=_durations(mp4), want_s=CLIP_DUR, want_frames=round(CLIP_DUR * fps))
    det = _single_pass_detector()
    inp, trim = seek.one_stream(media, CLIP_T0)
    head = f"[0:v]trim=start={trim:.3f}:duration={CLIP_DUR:.3f},setpts=PTS-STARTPTS,fps={fps:g}"
    sw, sh = PANEL_W // 4, PANEL_H // 4
    cam_ps, game_ps, frame_ps, rf_x = [], [], [], []
    faces = []  # per frame: [face in the Short (panel px) | None, face in the true cam (source px) | None]
    bars = {"cam": 0, "game": 0, "src-cam": 0, "src-game": 0}
    n = 0

    def grey(a):
        return cv2.cvtColor(np.ascontiguousarray(a), cv2.COLOR_RGB2GRAY)

    def black(strips, src_strips) -> str | None:
        hit = [
            float(e.mean()) < 6
            for a, e in zip(strips, src_strips, strict=True)
            if a.mean() < 5 and a.std() < 2
        ]
        return None if not hit else ("src" if all(hit) else "ours")

    def edge(a, k):
        return [a[:k], a[-k:], a[:, :k], a[:, -k:]]

    if mode == "split":
        cx, cy, cw, ch = shown["cam"]
        gx, gy, gw, gh = shown["game"]
        tx, ty, tw, th = gt_cam["rect"] if gt_cam else shown["cam"]
        tk = 240 / th
        tww = max(2, round(tw * tk) // 2 * 2)
        src = _stream(
            inp,
            f"{head},split=3[a][b][c];[a]crop={cw}:{ch}:{cx}:{cy},scale={sw}:{sh}[ca];"
            f"[b]crop={gw}:{gh}:{gx}:{gy},scale={sw}:{sh}[ga];[c]crop={tw}:{th}:{tx}:{ty},scale={tww}:240[tc];"
            "[ca][ga][tc]hstack=inputs=3[o]",
            2 * sw + tww, sh,
        )  # fmt: skip
        out = _stream(
            ["-i", str(mp4)],
            f"[0:v]split=3[a][b][c];[a]crop=1080:{PANEL_H}:0:0,scale=540:480[top];"
            f"[b]crop=1080:{PANEL_H}:0:0,scale={sw}:{sh}[cs];[c]crop=1080:{PANEL_H}:0:{PANEL_H},scale={sw}:{sh}[gs];"
            "[cs][gs]vstack[small];[top][small]hstack[o]",
            540 + sw, 480,
        )  # fmt: skip
        mask_cam = np.ones((sh, sw), bool)
        x0, y0, x1, y1 = (v // 4 for v in LOGO_BOX)
        mask_cam[y0:y1, x0:x1] = False
        mask_game = np.ones((sh, sw), bool)
        x0, y0, x1, y1 = (v // 4 for v in CREDIT_BOX)
        mask_game[y0 - sh : y1 - sh, x0:x1] = False
        for o, s_ in zip(out, src, strict=False):
            n += 1
            top, small = o[:, :540], grey(o[:, 540:])
            exp_c, exp_g = grey(s_[:, :sw]), grey(s_[:, sw : 2 * sw])
            act_c, act_g = small[:sh], small[sh:]
            cam_ps.append(round(psnr(act_c, exp_c, mask_cam), 1))
            game_ps.append(round(psnr(act_g, exp_g, mask_game), 1))
            for name, act, exp in (("cam", act_c, exp_c), ("game", act_g, exp_g)):
                kind = black(edge(act, 3), edge(exp, 3))
                if kind:
                    bars[name if kind == "ours" else "src-" + name] += 1
            f_out = _best_face(det(top), 2.0)
            f_src = _best_face(det(s_[:, 2 * sw :]), 1.0 / tk)
            in_src = (
                [round(f_src[0] + tx), round(f_src[1] + ty), round(f_src[2]), round(f_src[3])]
                if f_src
                else None
            )
            faces.append([[round(v) for v in f_out] if f_out else None, in_src])
    elif mode == "frame":
        x, y, w, h = tpl.fit_crop((0, 0, fw, fh), PANEL_W / tpl.FRAME_H)
        fq = tpl.FRAME_H // 4 // 2 * 2
        src = _stream(inp, f"{head},crop={w}:{h}:{x}:{y},scale={sw}:{fq}[o]", sw, fq)
        out = _stream(
            ["-i", str(mp4)], f"[0:v]crop=1080:{tpl.FRAME_H}:0:{tpl.FRAME_Y},scale={sw}:{fq}[o]", sw, fq
        )
        for o, s_ in zip(out, src, strict=False):
            n += 1
            frame_ps.append(round(psnr(grey(o), grey(s_)), 1))
    else:  # reframe: slide the Short's picture over the source to find the window
        cwid = int(fh * 9 / 16) // 2 * 2
        qw, qh = fw // 4 // 2 * 2, 270
        ww = round(cwid * qw / fw)
        src = _stream(inp, f"{head},scale={qw}:{qh}[o]", qw, qh)
        out = _stream(["-i", str(mp4)], "[0:v]scale=360:640[o]", 360, 640)
        band = slice(40, 210)  # clear of the logo, the credit and the captions
        for o, s_ in zip(out, src, strict=False):
            n += 1
            a = cv2.resize(grey(o), (ww, qh), interpolation=cv2.INTER_AREA).astype(np.float32)[band]
            b = grey(s_).astype(np.float32)[band]
            err = [float(np.mean((b[:, xx : xx + ww] - a) ** 2)) for xx in range(0, qw - ww + 1)]
            bx = int(np.argmin(err))
            rf_x.append(round(bx * fw / qw))
            frame_ps.append(round(10 * math.log10(255.0**2 / max(err[bx], 1e-6)), 1))
            f_out = _best_face(det(o), 3.0)
            faces.append([[round(v) for v in f_out] if f_out else None, None])
    res.update(n_frames=n, cam_psnr=cam_ps, game_psnr=game_ps, frame_psnr=frame_ps, faces=faces, bars=bars,
               reframe_x=rf_x, shown=shown, gt_cam=gt_cam["rect"] if gt_cam else None, ok=True)  # fmt: skip
    return res


def _best_face(boxes, k: float):
    if not boxes:
        return None
    b = max(boxes, key=lambda b: b[2] * b[3])
    return tuple(v * k for v in b)


def _whole(face, w: int, h: int, pad: float = 0.12) -> bool:
    """The face box, grown by pad for forehead and chin, lies inside the w x h picture."""
    x, y, fw, fh = face
    return x - pad * fw >= 0 and y - 2 * pad * fh >= 0 and x + fw * (1 + pad) <= w and y + fh * (1 + pad) <= h


def cmd_frames(root: Path, logins: list[str], tag: str, jobs: int) -> None:
    out = root / "frames" / tag
    out.mkdir(parents=True, exist_ok=True)
    work = [
        (str(root), s, tag) for s in samples(root, logins) if (root / "render" / tag / f"{s}.json").exists()
    ]
    with ProcessPoolExecutor(jobs) as ex:
        for r in ex.map(_frames_one, work):
            (out / f"{r['login']}.json").write_text(json.dumps(r))
            print(r["login"], r.get("mode"), r.get("n_frames"), r.get("error", ""), flush=True)


# --------------------------------------------------------------------------- sharpness
def ssim(a, b) -> float:
    """Mean structural similarity of two grey images (11 px gaussian windows)."""
    import cv2
    import numpy as np

    a, b = a.astype(np.float32), b.astype(np.float32)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2

    def blur(x):
        return cv2.GaussianBlur(x, (11, 11), 1.5)

    mu_a, mu_b = blur(a), blur(b)
    va, vb, cov = blur(a * a) - mu_a**2, blur(b * b) - mu_b**2, blur(a * b) - mu_a * mu_b
    m = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a**2 + mu_b**2 + c1) * (va + vb + c2))
    return float(m[8:-8, 8:-8].mean())


def detail(img) -> float:
    """Variance of the Laplacian: how much fine detail (and noise) a grey picture holds."""
    import cv2

    return float(cv2.Laplacian(img, cv2.CV_32F, ksize=3)[4:-4, 4:-4].var())


def _sharp_one(args: tuple[str, str, str]) -> dict:
    root_s, login, tag = args
    import cv2

    root = Path(root_s)
    mp4 = root / "render" / tag / f"{login}.mp4"
    meta = json.loads(mp4.with_suffix(".json").read_text())
    res: dict = {"login": login, "ok": False}
    if not meta.get("ok") or meta["layout"]["shown"]["mode"] != "split":
        return res
    fw, fh = meta["wh"]
    cx, cy, cw, ch = meta["layout"]["shown"]["cam"]
    rows = []
    src_it = source_frames(root / "rec" / f"{login}.ts", CLIP_T0, CLIP_DUR, meta["fps"], fw, fh)
    for k, (o, s_) in enumerate(zip(short_frames(mp4), src_it, strict=False)):
        if k % SHARP_EVERY != SHARP_EVERY // 2:
            continue
        crop = s_[cy : cy + ch, cx : cx + cw]
        ref = cv2.resize(crop, (PANEL_W, PANEL_H), interpolation=cv2.INTER_LANCZOS4)  # a clean enlargement
        act = o[:PANEL_H].copy()
        x0, y0, x1, y1 = LOGO_BOX
        act[y0:y1, x0:x1] = ref[y0:y1, x0:x1]  # the logo is not the picture
        rows.append((psnr(act, ref), ssim(act, ref), detail(act) / max(detail(ref), 1e-6), detail(crop)))
    if not rows:
        return res
    med = [statistics.median(r[i] for r in rows) for i in range(4)]
    res.update(ok=True, frames=len(rows), psnr=round(med[0], 2), ssim=round(med[1], 4),
               detail_kept=round(med[2], 3), source_detail=round(med[3], 1), enlarge=round(PANEL_W / cw, 2),
               crop=[cw, ch], source=[fw, fh])  # fmt: skip
    return res


def cmd_sharp(root: Path, logins: list[str], tag: str, jobs: int) -> None:
    """What the cam panel loses on the way from the recording to the Short, against a clean (Lanczos)
    enlargement of the very crop it shows: PSNR / SSIM, and the share of fine detail kept."""
    work = [
        (str(root), s, tag) for s in samples(root, logins) if (root / "render" / tag / f"{s}.json").exists()
    ]
    with ProcessPoolExecutor(jobs) as ex:
        rows = [r for r in ex.map(_sharp_one, work) if r.get("ok")]
    out = root / "frames" / tag
    out.mkdir(parents=True, exist_ok=True)
    (out / "sharp.json").write_text(json.dumps(rows, indent=1))
    for r in sorted(rows, key=lambda r: r["enlarge"]):
        crop = f"{r['crop'][0]}x{r['crop'][1]} of {r['source'][1]}p"
        print(f"  {r['login']:18s} x{r['enlarge']:<5} crop {crop}  "
              f"psnr {r['psnr']:5.2f}  ssim {r['ssim']:.4f}  detail kept {r['detail_kept']:.2f}")  # fmt: skip
    if rows:
        print(json.dumps({
            "set": tag, "shorts": len(rows), "enlarge_median": statistics.median(r["enlarge"] for r in rows),
            "enlarge_max": max(r["enlarge"] for r in rows),
            "psnr_median": statistics.median(r["psnr"] for r in rows),
            "ssim_median": statistics.median(r["ssim"] for r in rows),
            "detail_kept_median": statistics.median(r["detail_kept"] for r in rows),
        }, indent=1))  # fmt: skip


# --------------------------------------------------------------------------- labels
def load_label(root: Path, login: str) -> dict | None:
    p = root / "gt" / "labels" / f"{login}.json"
    return json.loads(p.read_text()) if p.exists() else None


def frame_times(root: Path, login: str) -> list[float]:
    p = root / "gt" / "frames" / login / "index.json"
    return [t for t, _ in json.loads(p.read_text())] if p.exists() else []


def regime_at(gt: dict, root: Path, login: str, t: float) -> tuple[str, list[dict]]:
    """(kind, cams) the label gives for media time t. A label may hold 'scenes': [{frames:[a,b], kind,
    cams}] for samples whose scene switches; frames index the 12 labelled frames, and t goes with the
    nearest of them unless the scene gives its own seconds ('t': [from, to], read off the recording)."""
    times = frame_times(root, login)
    scenes = gt.get("scenes")
    for sc in scenes or []:
        if "t" in sc and sc["t"][0] <= t < sc["t"][1]:
            return sc["kind"], sc.get("cams", [])
    if scenes and times:
        i = min(range(len(times)), key=lambda j: abs(times[j] - t))
        for sc in scenes:
            a, b = sc["frames"]
            if a <= i <= b:
                return sc["kind"], sc.get("cams", [])
    return gt["kind"], gt.get("cams", [])


def cam_at(gt: dict, root: Path, login: str, t: float) -> dict | None:
    kind, cams = regime_at(gt, root, login, t)
    if kind != "overlay" or not cams:
        return None
    return next((c for c in cams if c.get("who", "main") == "main"), cams[0])


def ideal_crop(rect: list[int], head: list[int] | None) -> tuple[float, float, float, float]:
    x, y, w, h = rect
    a = PANEL_W / PANEL_H
    cw, ch = (h * a, h) if w / h > a else (w, w / a)
    mx = head[0] + head[2] / 2 if head else x + w / 2
    my = head[1] + head[3] / 2 if head else y + h / 2
    return min(max(mx - cw / 2, x), x + w - cw), min(max(my - ch / 2, y), y + h - ch), cw, ch


def iou(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    u = a[2] * a[3] + b[2] * b[3] - ix * iy
    return ix * iy / u if u > 0 else 0.0


def score_moment(m: dict, kind: str, cams: list[dict], head: list[int] | None) -> dict:
    """One layout decision against the label. fails = list of failure kinds (empty = right).

    overlay, rectangular cam: the shown crop lies inside the labelled picture (SPILL_PX) and is not much
    tighter than the largest crop that picture allows (ZOOM_MAX). Cut-out and round cams have no rectangle
    to stay inside: the shown crop must hold the labelled head and overlap the person's box (LOOSE_IOU).
    avatar (VTuber): no webcam is right, and so is a "cam" on the avatar; a cam anywhere else is false.
    A cam inside watched content (label in_content) is content: choosing it is wrong, showing it in the
    gameplay panel is not a duplicate."""
    fails: list[str] = []
    out: dict = {"kind": kind, "source": m.get("source")}
    if "error" in m:
        return {**out, "fails": ["error"]}
    has = bool(m["has_webcam"])
    own = [c for c in cams if not c.get("in_content")]
    if kind == "overlay":
        if not has:
            return {**out, "fails": ["cam-missed"]}
        shown = m["shown"]["cam"]
        best = max(cams, key=lambda c: iou(shown, c["rect"]))
        if iou(shown, best["rect"]) <= 0.0:
            return {**out, "fails": ["cam-elsewhere"]}  # a face of the game, a poster ...
        if best.get("in_content"):
            return {**out, "fails": ["cam-of-content"]}
        out["who"] = best.get("who", "main")
        x, y, w, h = best["rect"]
        sx, sy, sw, sh = shown
        main = best.get("who", "main") == "main"
        if best.get("shape", "rect") == "rect":
            spill = max(x - sx, y - sy, sx + sw - (x + w), sy + sh - (y + h), 0)
            ideal = ideal_crop(best["rect"], head if main else None)
            zoom = ideal[3] / sh
            out.update(spill=round(spill), zoom=round(zoom, 2), enlarge=round(PANEL_W / sw, 2),
                       enlarge_ideal=round(PANEL_W / ideal[2], 2),
                       cam_iou=round(iou(_rect(m["cam"]), best["rect"]), 2))  # fmt: skip
            if spill > SPILL_PX:
                fails.append("spill")
            if zoom > ZOOM_MAX:
                fails.append("over-zoom")
        else:
            out.update(loose_iou=round(iou(shown, best["rect"]), 2), enlarge=round(PANEL_W / sw, 2))
            if iou(shown, best["rect"]) < LOOSE_IOU:
                fails.append("cutout-off")
        g = m["shown"]["game"]
        dup = max(_overlap(g, c["rect"]) / (g[2] * g[3]) for c in own)
        out["dup"] = round(dup, 3)
        if dup > 0.02:
            fails.append("cam-in-game")
        if head and main:
            hx, hy, hw, hh = head
            if hx < sx or hy < sy or hx + hw > sx + sw or hy + hh > sy + sh:
                out["head_cut"] = True  # the labelled head (mid frame) is not whole in the shown crop
    elif has and kind == "avatar" and cams and iou(m["shown"]["cam"], cams[0]["rect"]) >= LOOSE_IOU:
        out["avatar_as_cam"] = True
    elif has:
        fails.append("false-cam" if kind in ("none", "avatar") else "fullcam-as-overlay")
    elif kind == "fullcam" and not m.get("reframe"):
        fails.append("fullcam-boxed")
    return {**out, "fails": fails}


def three_way(m: dict, kind: str, cams: list[dict], head: list[int] | None, mode: str | None = None) -> tuple:
    """(right | safe | wrong, why) for one layout decision. The bar: a Short never shows the wrong thing as
    the webcam.

    right: the cam panel shows the streamer's cam and nothing else (score_moment's limits), a full-screen
    camera is followed full screen, a stream without a camera gets no cam panel. safe: the whole 16:9 frame
    where a webcam or a camera existed. wrong: anything else: another thing in the cam panel, a camera split
    in two, and a webcam stream reframed to a full-screen window (the cam may not even be in it).
    mode = how the Short was really rendered (split | frame | reframe) when known: a layout may ask for a
    reframe that the renderer does not do (no face to follow)."""
    r = score_moment(m, kind, cams, head)
    fails = [
        f for f in r["fails"] if f != "cam-in-game"
    ]  # a cam also in the gameplay panel: noted, not wrong
    has = bool(m.get("has_webcam"))
    reframed = (mode == "reframe") if mode else bool(m.get("reframe"))
    if kind == "overlay":
        if has:
            return ("right", "") if not fails else ("wrong", ",".join(fails))
        return ("wrong", f"reframed ({m.get('source')})") if reframed else ("safe", str(m.get("source")))
    if kind == "fullcam":
        if has:
            return "wrong", "fullcam-as-overlay"
        return ("right", "") if reframed else ("safe", str(m.get("source")))
    if has and not r.get("avatar_as_cam"):
        return "wrong", "false-cam"
    return "right", ("reframed" if reframed else "")


def _rect(s) -> tuple[int, int, int, int]:
    w, h, x, y = (int(float(v)) for v in str(s).split(":"))
    return x, y, w, h


def _overlap(a, b) -> float:
    ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy


def cmd_score(root: Path, logins: list[str], tag: str, backend: str) -> dict:
    rows, per = [], {}
    for s in samples(root, logins):
        gt = load_label(root, s)
        p = root / "det" / tag / f"{s}.{backend}.json"
        if not gt or gt.get("ad") or gt.get("exclude") or not p.exists():
            continue
        det = json.loads(p.read_text())
        ms = []
        for m in det["moments"]:
            kind, cams = regime_at(gt, root, s, m["peak"])
            r = score_moment(m, kind, cams, gt.get("head"))
            r["verdict"], r["why"] = three_way(m, kind, cams, gt.get("head"))
            r.update(login=s, peak=m["peak"])
            ms.append(r)
            rows.append(r)
        per[s] = ms
    n = len(rows)
    bad = [r for r in rows if r["fails"]]
    kinds: dict[str, int] = {}
    for r in bad:
        for f in r["fails"]:
            kinds[f] = kinds.get(f, 0) + 1
    chans_bad = sorted({r["login"] for r in bad})
    three = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("right", "safe", "wrong")}
    reasons: dict[str, int] = {}
    for r in rows:
        if r["verdict"] != "right":
            key = f"{r['verdict']}: {r['why']}"
            reasons[key] = reasons.get(key, 0) + 1
    summary = {
        "set": tag, "backend": backend, "channels": len(per), "moments": n, **three, "reasons": reasons,
        "moments_right": n - len(bad),
        "channels_all_right": len(per) - len(chans_bad), "fail_kinds": kinds, "channels_failing": chans_bad,
    }  # fmt: skip
    zs = [r["zoom"] for r in rows if "zoom" in r]
    en = [r["enlarge"] for r in rows if "enlarge" in r]
    if zs:
        summary["zoom_median"] = statistics.median(zs)
        summary["enlarge_median"] = statistics.median(en)
        summary["enlarge_ideal_median"] = statistics.median(r["enlarge_ideal"] for r in rows if "zoom" in r)
    out = root / "det" / tag / f"score.{backend}.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    print(json.dumps(summary, indent=1))
    for s, ms in per.items():
        if any(m["verdict"] != "right" for m in ms):
            print(
                f"  {s:22s}", " | ".join(f"{m['peak']:.0f}s {m['verdict']} {m['why']}".rstrip() for m in ms)
            )
    for s, ms in per.items():
        if VERBOSE and any(m["fails"] for m in ms):
            print(f"  {s:22s}", " | ".join(
                f"{m['peak']:.0f}s {m['source']} {','.join(m['fails']) or 'ok'}"
                + (f" sp{m['spill']} z{m['zoom']}" if "spill" in m else "") for m in ms))  # fmt: skip
    return summary


# --------------------------------------------------------------------------- frames -> verdict
def _inside(face, rect, pad: float = 0.12) -> bool:
    """A face box (x, y, w, h), grown a little for forehead and chin, lies inside rect (x, y, w, h)."""
    x, y, w, h = face
    rx, ry, rw, rh = rect
    return (
        x - pad * w >= rx
        and y - 2 * pad * h >= ry
        and x + w * (1 + pad) <= rx + rw
        and y + h * (1 + pad) <= ry + rh
    )


def frame_verdict(r: dict) -> list[str]:
    """Failures of one Short from its per-frame record (empty = clean)."""
    fails: list[str] = []
    if not r.get("ok"):
        return ["render-failed"]
    n = r["n_frames"]
    d = r["durations"]
    v, a = float(d["video"]["duration"]), float(d["audio"]["duration"])
    if abs(v - r["want_s"]) > 0.1 or abs(a - r["want_s"]) > 0.1:
        fails.append(f"length v{v:.2f} a{a:.2f}")
    if abs(n - r["want_frames"]) > 1:
        fails.append(f"frame-count {n}/{r['want_frames']}")
    for name in ("cam_psnr", "game_psnr", "frame_psnr"):
        low = sum(1 for x in r[name] if x < PSNR_MIN)
        if low:
            fails.append(f"{name}<{PSNR_MIN:g}:{low}f")
    for name in ("cam", "game"):
        if r["bars"][name]:
            fails.append(f"black-{name}:{r['bars'][name]}f")
    if r["mode"] == "split":
        # the source shows the face whole inside the true cam, the crop that is shown does not hold it
        true_cam, crop = r.get("gt_cam"), r["shown"]["cam"]
        cut = [
            k for k, (_fo, fs) in enumerate(r["faces"])
            if fs and true_cam and _inside(fs, true_cam) and not _inside(fs, crop, pad=0.0)
        ]  # fmt: skip
        if cut:
            fails.append(f"face-cut:{len(cut)}f")
        seen_src = sum(1 for _fo, fs in r["faces"] if fs)
        seen_out = sum(1 for fo, _fs in r["faces"] if fo)
        if seen_out < 0.5 * n and seen_src >= 0.5 * n:
            fails.append(f"no-face-in-panel:{n - seen_out}f")
    if r["mode"] == "reframe" and len(r["reframe_x"]) > 2:
        xs = r["reframe_x"]
        jump = max(abs(b - a) for a, b in zip(xs, xs[1:], strict=False))
        if jump > REFRAME_JUMP:
            fails.append(f"reframe-jump:{jump}px")
    return fails


def cmd_frames_score(root: Path, logins: list[str], tag: str) -> dict:
    tot = {"channels": 0, "frames": 0}
    kinds: dict[str, int] = {}
    chans: dict[str, list[str]] = {}
    modes: dict[str, int] = {}
    info = {"src_black_game": 0, "faces_in_panel": 0, "split_frames": 0}
    for s in samples(root, logins):
        p = root / "frames" / tag / f"{s}.json"
        gt = load_label(root, s)
        if not p.exists() or not gt or gt.get("ad") or gt.get("exclude"):
            continue
        r = json.loads(p.read_text())
        tot["channels"] += 1
        tot["frames"] += r.get("n_frames", 0)
        if r.get("ok"):
            modes[r["mode"]] = modes.get(r["mode"], 0) + 1
            info["src_black_game"] += r["bars"]["src-game"]
            if r["mode"] == "split":
                info["split_frames"] += r["n_frames"]
                info["faces_in_panel"] += sum(1 for fo, _fs in r["faces"] if fo)
        fails = frame_verdict(r)
        if fails:
            chans[s] = fails
            for f in fails:
                k = f.split(":")[0].split(" ")[0]
                kinds[k] = kinds.get(k, 0) + 1
    summary = {"set": tag, **tot, "channels_clean": tot["channels"] - len(chans), "modes": modes,
               "fail_kinds": kinds, **info}  # fmt: skip
    (root / "frames" / tag / "score.json").write_text(
        json.dumps({"summary": summary, "channels": chans}, indent=1)
    )
    print(json.dumps(summary, indent=1))
    for s, f in chans.items():
        print(f"  {s:22s}", "; ".join(f))
    return summary


CROP_FAULTS = ("face-cut", "reframe-jump")  # the right thing is shown, its crop is off in some frames


def cmd_shorts(root: Path, logins: list[str], tag: str) -> dict:
    """Every rendered Short: right, safe or wrong? The layout it was rendered with against the label at
    the render's moment and at every labelled frame inside the clip, how it was really rendered, and
    every frame of it (frame_verdict): a panel that does not show the crop the layout names, a black
    panel, a wrong length are wrong. A face partly outside the panel (the 1080x960 panel shows two
    thirds of a 16:9 cam) and a jump of the full-screen window are counted beside the verdict as
    crop faults: the right thing is on screen."""
    counts = {"right": 0, "safe": 0, "wrong": 0}
    frames = 0
    rows, faults = [], {}
    for s in samples(root, logins):
        gt = load_label(root, s)
        mp, fp = root / "render" / tag / f"{s}.json", root / "frames" / tag / f"{s}.json"
        if not gt or gt.get("ad") or gt.get("exclude") or not mp.exists() or not fp.exists():
            continue
        meta, fr = json.loads(mp.read_text()), json.loads(fp.read_text())
        frames += fr.get("n_frames", 0)
        kind, cams = regime_at(gt, root, s, CLIP_PEAK)
        lay = dict(meta.get("layout") or {})
        if not meta.get("ok") or not fr.get("ok"):
            verdict, why = "wrong", "render failed"
        else:
            # one layout for the whole clip: it is judged at the moment and at every labelled frame that
            # lies inside the clip (a layout the streamer switches away from is wrong from there on)
            at = [CLIP_PEAK, *(t for t in frame_times(root, s) if CLIP_T0 <= t <= CLIP_T0 + CLIP_DUR)]
            seen = [three_way(lay, *regime_at(gt, root, s, t), gt.get("head"), mode=fr["mode"]) for t in at]
            verdict, why = min(seen, key=lambda v: ("wrong", "safe", "right").index(v[0]))
            bad = frame_verdict(fr)
            hard = [b for b in bad if not b.startswith(CROP_FAULTS)]
            if hard:
                verdict, why = "wrong", "; ".join([why, *hard]).strip("; ")
            if len(hard) < len(bad):
                faults[s] = [b for b in bad if b.startswith(CROP_FAULTS)]
        counts[verdict] += 1
        if verdict != "right" or s in faults:
            rows.append(
                (s, kind, str(lay.get("source")), verdict, "; ".join([why, *faults.get(s, [])]).strip("; "))
            )
    summary = {
        "set": tag, "shorts": sum(counts.values()), "frames": frames, **counts,
        "right_with_crop_fault": sum(1 for r in rows if r[3] == "right"),
        "unsure": {r[0]: r[4] for r in rows if r[2] == "unsure"},
    }  # fmt: skip
    (root / "frames" / tag / "shorts.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=1)
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "unsure"}))
    for login, kind, source, verdict, why in rows:
        print(f"  {login:18s} {kind:8s} {source:14s} {verdict:6s} {why}")
    return summary


# --------------------------------------------------------------------------- gallery
def _short_frame(mp4: Path, t: float, out: Path) -> bool:
    from clipwatcher import config

    r = subprocess.run(
        [config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(mp4),
         "-frames:v", "1", str(out)],
        capture_output=True,
    )  # fmt: skip
    return r.returncode == 0 and out.exists()


def _gallery_one(args: tuple[str, str, str]) -> str:
    root_s, login, tag = args
    from PIL import Image, ImageDraw

    from clipwatcher import calibrate

    root = Path(root_s)
    mp4 = root / "render" / tag / f"{login}.mp4"
    meta = json.loads(mp4.with_suffix(".json").read_text())
    out = root / "gallery" / tag
    out.mkdir(parents=True, exist_ok=True)
    gt = load_label(root, login)
    tmp = out / f".{login}_src.png"
    if not calibrate.grab_frame(root / "rec" / f"{login}.ts", CLIP_PEAK, tmp):
        return login + " (no source frame)"
    src = Image.open(tmp).convert("RGB")
    d = ImageDraw.Draw(src)
    lw = max(3, src.width // 400)
    if gt:  # what a person sees: green
        for c in regime_at(gt, root, login, CLIP_PEAK)[1]:
            x, y, w, h = c["rect"]
            d.rectangle([x - lw, y - lw, x + w + lw - 1, y + h + lw - 1], outline=(0, 255, 0), width=lw)
    lay = meta.get("layout") or {}
    shown = lay.get("shown") or {}
    if shown.get("mode") == "split":  # what the code shows: red = cam panel, blue = gameplay panel
        for key, col in (("cam", (255, 40, 40)), ("game", (60, 140, 255))):
            x, y, w, h = shown[key]
            d.rectangle([x, y, x + w - 1, y + h - 1], outline=col, width=lw)
        if lay.get("cam"):  # the rectangle found (thin, yellow) when the shown crop is a part of it
            x, y, w, h = _rect(lay["cam"])
            d.rectangle([x, y, x + w - 1, y + h - 1], outline=(255, 230, 0), width=1)
    held_back = (lay.get("evidence") or {}).get("rect") if lay.get("source") == "unsure" else None
    if held_back:  # the rectangle an unsure layout did not show as the webcam: orange
        x, y, w, h = held_back
        d.rectangle([x, y, x + w - 1, y + h - 1], outline=(255, 150, 0), width=lw)
    tmp.unlink(missing_ok=True)
    left = src.resize((960, round(960 * src.height / src.width)), Image.LANCZOS)
    sheet = Image.new("RGB", (960 + 8 + 304 + 8 * (216 + 4), 580), (18, 18, 22))
    sheet.paste(left, (0, 40))
    cells = [CLIP_PEAK - CLIP_T0] + [CLIP_DUR * (i + 0.5) / 8 for i in range(8)]
    for i, t in enumerate(cells):
        f = out / f".{login}_{i}.png"
        if meta.get("ok") and _short_frame(mp4, t, f):
            im = Image.open(f).convert("RGB")
            if i == 0:
                sheet.paste(im.resize((304, 540), Image.LANCZOS), (968, 40))
            else:
                sheet.paste(im.resize((216, 384), Image.LANCZOS), (968 + 304 + 8 + (i - 1) * 220, 40))
            f.unlink(missing_ok=True)
    kind = regime_at(gt, root, login, CLIP_PEAK)[0] if gt else "?"
    text = (
        f"{login}   label: {kind}   layout: {lay.get('source')} ({shown.get('mode')})   "
        "green = labelled cam, red = cam panel crop, blue = gameplay panel crop, orange = held back   |   "
        "Short at the same moment, then 8 frames across it"
    )
    ImageDraw.Draw(sheet).text((8, 6), text, fill=(235, 235, 235))
    if lay.get("doubts"):
        ImageDraw.Draw(sheet).text((8, 22), "unsure: " + "; ".join(lay["doubts"])[:230], fill=(255, 170, 60))
    sheet.save(out / f"{login}.jpg", quality=88)
    return login


def cmd_gallery(root: Path, logins: list[str], tag: str, jobs: int) -> None:
    """Per sample one picture: the source frame at the moment with the labelled and the shown rectangles,
    the Short at that moment beside it, and eight frames across the Short. Plus index.html."""
    work = [
        (str(root), s, tag) for s in samples(root, logins) if (root / "render" / tag / f"{s}.json").exists()
    ]
    with ProcessPoolExecutor(jobs) as ex:
        done = list(ex.map(_gallery_one, work))
    out = root / "gallery" / tag
    rows = "".join(
        f'<h3>{n}</h3><img src="{n}.jpg" style="max-width:100%">' for n in sorted(done) if " " not in n
    )
    (out / "index.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>webcam check {tag}</title>"
        f"<body style='background:#111;color:#eee;font-family:sans-serif'>{rows}</body>"
    )
    print(f"{len(done)} sheets in {out}")


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    steps = ["detect", "render", "frames", "score", "frames-score", "shorts", "sharp", "gallery"]
    ap.add_argument("step", choices=steps)
    ap.add_argument("root", type=Path)
    ap.add_argument("logins", nargs="*")
    ap.add_argument("--set", default="base")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--backend", default="mediapipe")
    a = ap.parse_args()
    if a.step == "detect":
        cmd_detect(a.root, a.logins, a.set, a.jobs, a.backend.split(","))
    elif a.step == "render":
        cmd_render(a.root, a.logins, a.set, a.jobs)
    elif a.step == "frames":
        cmd_frames(a.root, a.logins, a.set, a.jobs)
    elif a.step == "score":
        cmd_score(a.root, a.logins, a.set, a.backend)
    elif a.step == "frames-score":
        cmd_frames_score(a.root, a.logins, a.set)
    elif a.step == "shorts":
        cmd_shorts(a.root, a.logins, a.set)
    elif a.step == "sharp":
        cmd_sharp(a.root, a.logins, a.set, a.jobs)
    elif a.step == "gallery":
        cmd_gallery(a.root, a.logins, a.set, a.jobs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
