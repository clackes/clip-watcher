"""Animated caption overlays rendered with Remotion (the `rm:<template>` caption styles).

The Remotion project lives in <repo>/remotion. Python decides everything about a clip's captions (pages,
timing, colours: render/rm_plan.py and render/palette.py), writes it as one props JSON and asks Node to
render a transparent PNG sequence of the caption band, which ffmpeg then lays over the Short.

Nothing here may break a clip: every failure raises RemotionError, and render/template.py falls back to
the ASS caption style. Nothing is fetched at render time: `available()` is false until `npm ci` and
`npm run setup` (headless browser + bundle) have been run in remotion/.

Config (config/global.yaml, all optional, defaults in code):
  render.captions: rm:<template>     use a Remotion template instead of an ASS style
  render.rm.colors: auto | fixed     auto picks colours for the video under the captions (default auto)
  render.rm.fallback: word           the ASS style used when Remotion cannot render
  render.rm.max_fps: 60              overlay frame rate cap (the overlay runs at the clip's own rate)
  render.rm.concurrency: null        browser tabs rendering in parallel (null = all cores);
                                     env CLIPWATCHER_RM_CONCURRENCY wins
  render.rm.timeout_s: 300           give up on a render after this long
  render.rm.templates: {name: {...}} parameter overrides per template, merged into
                                     remotion/templates/<name>.json
  render.rm.presets: {id: {name, base, settings, created}}
                                     this machine's own templates: rm:<id> is the template `base` as it
                                     shipped with `settings` laid over it (the companion app's Library)
  tools.node: /path/to/node          (or CLIPWATCHER_NODE)
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from clipwatcher import config
from clipwatcher.models import Word
from clipwatcher.render import palette, rm_plan
from clipwatcher.render.branding import FONTS_DIR

log = logging.getLogger(__name__)

PREFIX = "rm:"
REMOTION_DIR = Path(os.environ.get("CLIPWATCHER_REMOTION", config.ROOT / "remotion"))
TEMPLATES_DIR = REMOTION_DIR / "templates"
BUNDLE_DIR = REMOTION_DIR / "build"
# Emoji (owner, 20 Nov): Apple's and no others. Apple Color Emoji ships with macOS and may not be
# redistributed, so it is used where the system has it and never bundled; on any other machine emoji are
# taken out of the caption text rather than drawn with another emoji set.
EMOJI_FAMILY = "Apple Color Emoji"
EMOJI_FILE = Path(os.environ.get("CLIPWATCHER_EMOJI_FONT", "/System/Library/Fonts/Apple Color Emoji.ttc"))
FRAME_W, FRAME_H = 1080, 1920
DEAD_ZONE_Y = 1536  # nothing may be drawn at or below this line (platform UI)
SAFE_X = 60  # nor closer than this to the left and right edge
# label colours per beat kind, first choice first (the ASS style's: fail red, hype green, shock white,
# funny yellow)
LABEL_COLOURS = {
    "fail": ["red", "orange", "pink"],
    "hype": ["lime", "mint", "cyan"],
    "shock": ["white", "cream"],
    "funny": ["yellow", "gold", "orange"],
    "action": ["white", "cream"],
}


class RemotionError(RuntimeError):
    """The overlay could not be made. Callers fall back to ASS captions."""


# ------------------------------------------------------------------ settings and templates


_FOR_NOW: dict = {}  # render.rm for the renders inside `overriding`, laid over the config


def settings() -> dict:
    base = dict(config.global_config().get("render", {}).get("rm", {}) or {})
    return _merge(base, _FOR_NOW) if _FOR_NOW else base


@contextlib.contextmanager
def overriding(values: dict | None):
    """render.rm for the renders inside the block, laid over the config: what one video is rendered with
    when it is not what the config says (the companion app renders a clip again with its own colour mode
    and template parameters: {"colors": "fixed", "templates": {"punch": {"size": 80}}}). One render at a
    time: not for threads that render side by side."""
    global _FOR_NOW
    before = _FOR_NOW
    _FOR_NOW = _merge(before, values or {})
    try:
        yield
    finally:
        _FOR_NOW = before


def is_rm(style: str | None) -> bool:
    return bool(style) and str(style).startswith(PREFIX)


def template_name(style: str) -> str:
    return str(style)[len(PREFIX) :].strip()


def templates() -> list[str]:
    """Names of the caption templates that exist (remotion/templates/<name>.json)."""
    if not TEMPLATES_DIR.is_dir():
        return []
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.json") if not p.stem.startswith("_"))


def _merge(base: dict, over: dict) -> dict:
    """Deep merge. A block that names something else ({"name": "drop"} over {"name": "pop", "params": ...}:
    one animation swapped for another) replaces the old block whole: the old parameters are not its own."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        old = out.get(k)
        both = isinstance(v, dict) and isinstance(old, dict)
        if both and "name" in v and v["name"] != old.get("name"):
            out[k] = {"params": {}, **copy.deepcopy(v)}
        elif both:
            out[k] = _merge(old, v)
        else:
            out[k] = copy.deepcopy(v)
    return out


PRESET_ID = re.compile(r"[a-z0-9_]{1,40}")


def presets() -> dict[str, dict]:
    """This machine's own templates (render.rm.presets): id -> {"name", "base", "settings", "created"}. Each
    is a shipped template under a name of its own, with its own settings laid over the template as it
    shipped; its caption style is rm:<id>. An entry that cannot be one (an id that is a shipped template's
    name, a base that is not there) is left out."""
    raw = settings().get("presets")
    if not isinstance(raw, dict):
        return {}
    shipped = set(templates())
    out: dict[str, dict] = {}
    for pid, p in raw.items():
        ok = isinstance(pid, str) and PRESET_ID.fullmatch(pid) and pid not in shipped and isinstance(p, dict)
        if not ok or p.get("base") not in shipped:
            continue
        own = p.get("settings")
        try:
            created = float(p.get("created") or 0)
        except (TypeError, ValueError):
            created = 0.0
        out[pid] = {
            "name": str(p.get("name") or pid),
            "base": p["base"],
            "settings": copy.deepcopy(own) if isinstance(own, dict) else {},
            "created": created,
        }
    return out


def base_name(name: str) -> str:
    """The shipped template a name is drawn by: the name itself, or the base of one of this machine's own
    templates. This, never an own template's id, is what the renderer is asked for."""
    if name in templates():
        return name
    own = presets().get(name)
    return own["base"] if own else name


def load_template(name: str, overrides: dict | None = None) -> dict:
    """A template's parameters: its JSON, then render.rm.templates.<name> from the config, then overrides.
    For one of this machine's own templates (`presets`): the JSON of its base as it shipped, its own
    settings, then render.rm.templates.<its id> (what `overriding` sets for one render), then overrides."""
    own = None
    p = TEMPLATES_DIR / f"{name}.json"
    if name and not p.is_file():
        own = presets().get(name)
        if own:
            p = TEMPLATES_DIR / f"{own['base']}.json"
    if not name or not p.is_file():
        have = [*templates(), *presets()]
        raise RemotionError(f"unknown caption template {name!r} (have: {', '.join(have) or 'none'})")
    try:
        params = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RemotionError(f"template {name}: {e}") from e
    if own:
        params = _merge(params, own["settings"])
    params = _merge(params, (settings().get("templates") or {}).get(name) or {})
    return _merge(params, overrides or {})


def emoji_family() -> str:
    """CSS family of the emoji font on this machine: Apple Color Emoji, or "" where it is not installed."""
    return EMOJI_FAMILY if EMOJI_FILE.is_file() else ""


def emoji_set() -> frozenset[int]:
    """The emoji that may appear in captions here (empty off macOS)."""
    return rm_plan.emoji_chars(EMOJI_FILE) if emoji_family() else frozenset()


SLOTS = ("enter", "exit", "active", "emph", "idle")


def animations() -> dict[str, str]:
    """The animation library (remotion/src/core/animations.ts): name -> slot (enter | exit | active |
    emph | idle). Templates built from the library (punch) take any of them by name, per slot."""
    src = REMOTION_DIR / "src" / "core" / "animations.ts"
    if not src.is_file():
        return {}
    return dict(re.findall(r'name: "(\w+)",\s*slot: "(\w+)"', src.read_text(encoding="utf-8")))


def with_animation(params: dict, slot: str, name: str, by: str | None = None) -> dict:
    """Template parameters with one slot of the animation library set to another animation. by: page |
    word | letter for enter and exit (how the units take their turn)."""
    if animations().get(name) != slot:
        raise RemotionError(f"no {slot} animation {name!r}")
    ref: dict = {"name": name, "params": {}}
    if slot in ("enter", "exit"):
        ref["by"] = by or "page"
        if ref["by"] not in animation_units().get(name, ("page",)):
            raise RemotionError(f"{name} cannot run by {ref['by']}")
    out = copy.deepcopy(params)
    out["fx"][slot] = ref
    return out


def animation_units() -> dict[str, tuple[str, ...]]:
    """Enter and exit animations -> the units each suits (page, word, letter)."""
    src = REMOTION_DIR / "src" / "core" / "animations.ts"
    if not src.is_file():
        return {}
    found = re.findall(
        r'name: "(\w+)",\s*slot: "\w+",\s*units: \[([^\]]*)\]', src.read_text(encoding="utf-8")
    )
    return {name: tuple(re.findall(r'"(\w+)"', units)) for name, units in found}


def check_animations(timeout_s: float = 120) -> dict:
    """Sample every animation of the library (scripts/anim_check.mjs): rest states, continuity, 30 fps
    flicker limits, that every parameter does something and that no two animations of a slot move alike.
    Returns the script's report; raises RemotionError when it cannot run."""
    why = missing()
    if why:
        raise RemotionError(why)
    try:
        r = subprocess.run(
            [node_bin(), str(REMOTION_DIR / "scripts" / "anim_check.mjs")],
            cwd=REMOTION_DIR, capture_output=True, text=True, timeout=timeout_s,
        )  # fmt: skip
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        raise RemotionError(f"animation check failed to run: {e}") from e


def fallback_style() -> str:
    """The ASS style used when a Remotion template cannot be rendered."""
    from clipwatcher.render.captions import CAPTION_STYLES

    s = str(settings().get("fallback", "word"))
    return s if s in CAPTION_STYLES else "word"


def color_mode() -> str:
    m = str(settings().get("colors", "auto")).lower()
    return m if m in ("auto", "fixed") else "auto"


# ------------------------------------------------------------------ availability and the bundle


def node_bin() -> str | None:
    for c in (
        os.environ.get("CLIPWATCHER_NODE"),
        config.global_config().get("tools", {}).get("node"),
        "node",
    ):
        if c and (Path(c).exists() or shutil.which(c)):
            return shutil.which(c) or c
    return None


def _browser_installed() -> bool:
    d = REMOTION_DIR / "node_modules" / ".remotion"
    return d.is_dir() and any(d.rglob("chrome-headless-shell*"))


def missing() -> str | None:
    """Why Remotion captions cannot be rendered here, or None when everything is in place."""
    if not node_bin():
        return "node not found"
    if not (REMOTION_DIR / "scripts" / "render.mjs").is_file():
        return f"no Remotion project at {REMOTION_DIR}"
    if not (REMOTION_DIR / "node_modules" / "@remotion" / "renderer").is_dir():
        return "Node dependencies not installed (run `npm ci` in remotion/)"
    if not _browser_installed():
        return "headless browser not installed (run `npm run setup` in remotion/)"
    if not templates():
        return "no caption templates"
    return None


def available() -> bool:
    """True when a Remotion overlay can be rendered on this machine without fetching anything."""
    return missing() is None


def source_stamp() -> str:
    """Hash of everything the bundle is built from: sources, lockfile, font files."""
    h = hashlib.sha256()
    files = sorted((REMOTION_DIR / "src").rglob("*")) + sorted((REMOTION_DIR / "demo").glob("*.json"))
    files += [REMOTION_DIR / "package-lock.json", REMOTION_DIR / "scripts" / "bundle.mjs"]
    for f in files:
        if f.is_file():
            h.update(str(f.relative_to(REMOTION_DIR)).encode())
            h.update(f.read_bytes())
    for f in sorted(FONTS_DIR.rglob("*")):
        if f.is_file():
            h.update(f"{f.relative_to(FONTS_DIR)}:{f.stat().st_size}".encode())
    return h.hexdigest()[:20]


def bundle_stamp(bundle: Path | None = None) -> str | None:
    try:
        return json.loads(((bundle or BUNDLE_DIR) / "clipwatcher-bundle.json").read_text())["stamp"]
    except (OSError, ValueError, KeyError):
        return None


def ensure_bundle(bundle: Path | None = None) -> Path:
    """The bundle directory, built now if it is missing or older than the sources (about ten seconds,
    once). Renders reuse it, so a clip pays for rendering only."""
    bundle = bundle or BUNDLE_DIR
    why = missing()
    if why:
        raise RemotionError(why)
    stamp = source_stamp()
    if bundle_stamp(bundle) == stamp and (bundle / "index.html").is_file():
        return bundle
    cmd = [node_bin(), str(REMOTION_DIR / "scripts" / "bundle.mjs"), "--out", str(bundle), "--stamp", stamp]
    try:
        r = subprocess.run(cmd, cwd=REMOTION_DIR, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        raise RemotionError(f"bundle failed: {e}") from e
    if r.returncode != 0 or bundle_stamp(bundle) != stamp:
        raise RemotionError(f"bundle failed ({r.returncode}): {(r.stderr or r.stdout)[-1500:]}")
    log.info("remotion bundle built: %s", bundle)
    return bundle


# ------------------------------------------------------------------ where captions go


@dataclass(frozen=True)
class Box:
    """The part of the 1080x1920 frame captions may draw in. Ink never leaves it."""

    x0: int
    y0: int
    x1: int
    y1: int
    anchor_y: int  # the block is centred here when it fits

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0


def caption_box(cap_y: int, kind: str) -> Box:
    """Caption area for a Short layout. kind: seam (split, webcam over gameplay: captions on the seam,
    clear of the credit under it) | frame (no webcam: under the boxed frame, under its credit) |
    reframe (full-screen person: under the face) | stack (the old stack: under the panel)."""
    spans = {
        "seam": (-190, 66, 0),
        "frame": (-50, 80, 15),
        "reframe": (-170, 170, 0),
        "stack": (-69, 37, -17),
    }
    up, down, shift = spans.get(kind, spans["seam"])
    y1 = min(cap_y + down, DEAD_ZONE_Y - 4) // 2 * 2  # even rows: the overlay is blended in YUV
    y0 = max(0, min(cap_y + up, y1 - 80)) // 2 * 2
    return Box(SAFE_X, y0, FRAME_W - SAFE_X, y1, max(y0, min(y1, cap_y + shift)))


# ------------------------------------------------------------------ the video under the captions


class Background:
    """Small RGB frames of the caption box over the clip's time, for the auto colours."""

    def __init__(self, frames: list[bytes], fps: float):
        self.frames, self.fps = frames, fps

    def stats(self, t0: float, t1: float) -> palette.BgStats:
        """Statistics of the box from t0 to t1 (at least the nearest frame)."""
        if not self.frames:
            return palette.BgStats()
        a = max(0, min(len(self.frames) - 1, int(t0 * self.fps)))
        b = max(a + 1, min(len(self.frames), int(t1 * self.fps) + 1))

        def pixels():
            for f in self.frames[a:b]:
                for i in range(0, len(f) - 2, 3):
                    yield f[i], f[i + 1], f[i + 2]

        return palette.bg_stats(pixels())


BG_W, BG_H, BG_FPS = 48, 12, 5


def background_from_raw(raw: bytes, fps: float = BG_FPS, w: int = BG_W, h: int = BG_H) -> Background:
    n = w * h * 3
    return Background([raw[i : i + n] for i in range(0, len(raw) - n + 1, n)], fps)


def sample_video(video: Path, box: Box, *, fps: float = BG_FPS) -> Background:
    """The caption box of a finished 1080x1920 video (used for tests and the gallery; render_short samples
    its own picture before the captions go on)."""
    cmd = [
        config.ffmpeg_bin(), "-v", "error", "-i", str(video), "-an",
        "-vf", f"crop={box.w}:{box.h}:{box.x0}:{box.y0},fps={fps:g},scale={BG_W}:{BG_H}:flags=area",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]  # fmt: skip
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        raise RemotionError(f"background sampling failed: {e}") from e
    if r.returncode != 0:
        raise RemotionError(f"background sampling failed: {r.stderr[-500:]!r}")
    return background_from_raw(r.stdout, fps)


# ------------------------------------------------------------------ props


@dataclass
class Overlay:
    """A rendered caption overlay: PNG frames `pattern` in `dir`, to be laid over the video at (x, y)."""

    dir: Path
    pattern: str
    fps: float
    frames: int
    width: int
    height: int
    x: int
    y: int
    seconds: float = 0.0
    rendered: int = 0
    debug: Path | None = None
    gap_frames: int = 0  # frames in which the renderer closed a walled-in gap of video (scripts/gaps.mjs)
    gap_px: int = 0  # and how many pixels that was in all

    @property
    def input(self) -> str:
        return str(self.dir / self.pattern)


def _label_colors(scheme: dict, kind: str, auto: bool, stats: palette.BgStats) -> dict:
    names = LABEL_COLOURS.get(kind, LABEL_COLOURS["action"])
    pick = palette.resolve(names[0])
    if auto:
        ok = [
            c
            for c in (palette.resolve(n) for n in names)
            if palette.collides(c, stats) < palette.COLLIDE_SHARE
        ]
        ok = [c for c in ok if palette.contrast(c, scheme["stroke"]) >= palette.FLOOR]
        pick = ok[0] if ok else pick
    if palette.contrast(pick, scheme["stroke"]) < palette.FLOOR:
        pick = scheme["text"]
    return {**scheme, "text": pick, "active": pick}


def build_props(
    words: list[Word], dur_s: float, template: str, *, box: Box, fps: float, beats=None, actions=None,
    colors: str | None = None, background: Background | None = None, params: dict | None = None,
    pad: int = 0, debug: bool = False,
) -> tuple[dict, rm_plan.Plan]:  # fmt: skip
    """Props for one overlay, and the plan they were made from. colors: "fixed" | "auto" (None = config).
    template: a shipped template's name, or the id of one of this machine's own (`presets`).
    A fractional frame rate is fine (59.94): frame k shows the instant (k + 0.5) / fps.
    pad: extra transparent rows above and below the box (the frame checks use it to catch overflow)."""
    P = params if params is not None else load_template(template)
    mode = colors or color_mode()
    if mode not in ("auto", "fixed"):
        raise RemotionError(f"bad colour mode: {mode}")
    auto = mode == "auto" and background is not None
    mx, my = float(P["margin"]["x"]), float(P["margin"]["y"])
    pl = rm_plan.plan(
        words,
        dur_s,
        P,
        box_w=box.w - 2 * mx,
        box_h=box.h - 2 * my,
        beats=beats,
        actions=actions,
        emoji=emoji_set(),
    )
    fixed = {k: (list(v) if isinstance(v, list) else v) for k, v in P["colors"].items()}
    state = palette.AutoState()
    pairs = P["auto"].get("pairs", [])
    pages, prev, prev_end = [], None, -1e9
    for p in pl.pages:
        scheme = (
            palette.auto_colors(fixed, P["auto"], background.stats(p.start, p.end), state, p.start)
            if auto
            else fixed
        )
        eases = (
            prev is not None and prev != scheme and p.start - prev_end <= 0.15
            and palette.safe_transition(prev, scheme, pairs)
        )  # fmt: skip
        pages.append(
            {
                "id": p.id, "start": p.start, "end": p.end, "exit": p.exit,
                "words": [
                    dict(text=w.text, start=w.start, end=w.end, activeEnd=w.active_end, emph=w.emph)
                    for w in p.words
                ],
                "colors": scheme, "from": prev if eases else None,
            }
        )  # fmt: skip
        prev, prev_end = scheme, p.end
    labels = []
    for lb in pl.labels:
        st = background.stats(lb.start, lb.end) if auto else palette.BgStats()
        scheme = palette.auto_colors(fixed, P["auto"], st, palette.AutoState()) if auto else fixed
        labels.append({**asdict(lb), "colors": _label_colors(scheme, lb.kind, auto, st)})
    props = {
        "template": base_name(template),  # the component that draws it: an own template's base
        "params": P,
        "fps": fps,
        "width": FRAME_W,
        "height": box.h + 2 * pad,
        "durationInFrames": max(1, round(dur_s * fps)),
        "region": {
            "x0": box.x0,
            "y0": pad,
            "x1": box.x1,
            "y1": pad + box.h,
            "anchorY": pad + box.anchor_y - box.y0,
        },
        "pages": pages,
        "labels": labels,
        "emojiFamily": emoji_family(),
        "debug": debug,
    }
    return props, pl


# ------------------------------------------------------------------ rendering


def render_props(
    props: dict, out_dir: Path, *, y: int, debug: Path | None = None, all_frames: bool = False,
    concurrency: int | None = None, timeout_s: float | None = None, bundle: Path | None = None,
    scene_only: bool = False, raw: bool = False,
) -> Overlay:  # fmt: skip
    """Render props to a PNG sequence in out_dir. Raises RemotionError on any failure. scene_only: lay
    every frame out and write its scene to `debug`, draw nothing (no PNGs; for the frame checks). raw:
    the frames as the browser drew them, without the renderer's last step (gaps of video that the drawing
    walls in are closed): for the frame checks, to show that they see such a gap."""
    bundle = ensure_bundle(bundle)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pf = out_dir.parent / f"{out_dir.name}.props.json"
    pf.write_text(json.dumps(props), encoding="utf-8")
    conc = concurrency or os.environ.get("CLIPWATCHER_RM_CONCURRENCY") or settings().get("concurrency")
    cmd = [
        node_bin(),
        str(REMOTION_DIR / "scripts" / "render.mjs"),
        "--props",
        str(pf),
        "--out",
        str(out_dir),
    ]
    cmd += ["--bundle", str(bundle)]
    if conc:
        cmd += ["--concurrency", str(int(conc))]
    if debug:
        cmd += ["--debug", str(debug)]
    if all_frames:
        cmd += ["--all-frames"]
    if scene_only:
        cmd += ["--scene-only"]
    if raw:
        cmd += ["--raw"]
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            cmd, cwd=REMOTION_DIR, capture_output=True, text=True,
            timeout=timeout_s if timeout_s is not None else float(settings().get("timeout_s", 300)),
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        raise RemotionError(f"render failed: {e}") from e
    res = None
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            res = json.loads(line)
            break
        except ValueError:
            continue
    if r.returncode != 0 or not res or not res.get("ok"):
        why = (res or {}).get("error") or r.stderr[-1500:] or r.stdout[-1500:]
        raise RemotionError(f"render failed ({r.returncode}): {why}")
    n = int(res["frames"])
    drawn = scene_only or (out_dir / f"{n - 1:06d}.png").is_file()
    if n != props["durationInFrames"] or not drawn:
        raise RemotionError(f"render incomplete: {n} frames reported, {props['durationInFrames']} wanted")
    ov = Overlay(
        dir=out_dir, pattern=res["pattern"], fps=float(props["fps"]), frames=n, width=int(res["width"]),
        height=int(res["height"]), x=0, y=y, seconds=time.monotonic() - t0, rendered=int(res["rendered"]),
        debug=debug, gap_frames=int(res.get("gapFrames", 0)), gap_px=int(res.get("gapPx", 0)),
    )  # fmt: skip
    if res.get("gapFailed"):
        log.warning("remotion: %d frames could not be read for gap closing", res["gapFailed"])
    return ov


def render_overlay(
    words: list[Word], dur_s: float, template: str, out_dir: Path, *, box: Box, fps: float, beats=None,
    actions=None, colors: str | None = None, background: Background | None = None, params: dict | None = None,
) -> Overlay | None:  # fmt: skip
    """The caption overlay of a clip, or None when there is nothing to show. Raises RemotionError when
    Remotion is not set up or the render fails: the caller falls back to ASS captions."""
    why = missing()
    if why:
        raise RemotionError(why)
    fps = min(float(fps), float(settings().get("max_fps", 60)))
    props, pl = build_props(
        words, dur_s, template, box=box, fps=fps, beats=beats, actions=actions, colors=colors,
        background=background, params=params,
    )  # fmt: skip
    if pl.empty:
        return None
    return render_props(props, out_dir, y=box.y0)


TO_YUV = "scale=in_range=pc:out_range=tv:out_color_matrix=bt709:flags=accurate_rnd+full_chroma_int"


def overlay_filter(idx: int, ov: Overlay, src: str, out: str) -> str:
    """Filter graph piece that lays overlay input `idx` over the yuv420p video `src` -> `out`.

    Only the caption band is touched. The band is cut out, its chroma repeated to 4:4:4, the PNGs
    (straight-alpha sRGB, converted to the video's BT.709 limited range at full chroma resolution) are
    blended onto it, and the result is averaged back to 4:2:0 and put back in place. Blending in 4:4:4
    keeps coloured edges exact (blending subsampled straight-alpha chroma would grey them); repeating and
    averaging chroma is lossless where the overlay is transparent, so the rest of the band and the whole
    picture outside it stay bit-identical. The overlay's timestamps are moved exactly half a frame early
    (time base halved, one tick back): frame k of the overlay then lands on frame k of the video whatever
    the rounding of the video's own timestamps, and no time base finer than the video's is introduced."""
    return (
        f"[{idx}:v]settb=intb/2,setpts=PTS-1,format=rgba,{TO_YUV},format=yuva444p[rmov];"
        f"{src}split=2[rmmain][rmcut];"
        f"[rmcut]crop={ov.width}:{ov.height}:{ov.x}:{ov.y},scale=flags=neighbor,format=yuv444p[rmband];"
        "[rmband][rmov]overlay=x=0:y=0:format=yuv444:eof_action=pass:repeatlast=0,"
        "scale=flags=area,format=yuv420p[rmdone];"
        f"[rmmain][rmdone]overlay=x={ov.x}:y={ov.y}:format=yuv420:eof_action=pass{out}"
    )


def probe_filter(box: Box, fps: float = BG_FPS) -> str:
    """Filter chain that turns a 1080x1920 picture into the small samples `background_from_raw` reads."""
    return f"crop={box.w}:{box.h}:{box.x0}:{box.y0},fps={fps:g},scale={BG_W}:{BG_H}:flags=area,format=rgb24"


def overlay_input(ov: Overlay) -> list[str]:
    """ffmpeg input arguments for the overlay's PNG sequence."""
    return ["-framerate", f"{ov.fps:g}", "-start_number", "0", "-i", ov.input]
