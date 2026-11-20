"""The caption library of the companion app (D17, D19): every style a Short can be rendered with, what can
be set on each animated template, and a short clip that shows it moving.

The four built-in styles are burnt in by ffmpeg (render/captions.py). The animated templates are the
Remotion ones (render/remotion.py, `rm:<name>`): their parameters are in remotion/templates/<name>.json
and the limits of each in the template's own schema (remotion/src), which stays the authority. What is
read from those sources here is for the page (a slider needs its ends) and for refusing a value before
a render is started for it.

    python -m clipwatcher.ui.captions warm      # make the preview clip of every style now
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from clipwatcher import config
from clipwatcher.models import Word
from clipwatcher.render import palette
from clipwatcher.render import remotion as rm
from clipwatcher.render.captions import CAPTION_STYLES

log = logging.getLogger(__name__)

BUILT_IN = {
    "word": ("Word", "One word at a time on the seam. Shouted words turn yellow."),
    "pop": ("Pop", "Three words a line. The spoken word turns yellow and pops."),
    "classic": ("Classic", "A full line in the lower third, the current word highlighted."),
    "bounce": ("Bounce in", "Words bounce in, each in its own colour."),
}
SETUP = "cd remotion && npm ci && npm run setup"
COLOUR_MODES = ("auto", "fixed")
# never set from the page: names, the font files a template was drawn for, the auto colour rules
HIDDEN = ("title", "description", "font", "emphFont", "auto")
GROUPS = (
    ("text", "Text", ("size", "minSize", "case", "letterSpacing", "wordGap", "lineHeight", "emphScale")),
    ("paging", "Phrases", ("paging",)),
    ("timing", "Timing", ("timing",)),
    ("colors", "Colours", ("colors",)),
    ("motion", "Motion", ()),  # the animation slots of a template built from the library
    ("fx", "Effects", ("fx",)),
    ("label", "Sound labels", ("label",)),
    ("room", "Room around the words", ("grow", "tilt", "margin")),
)
LABELS = {
    "size": "Size",
    "minSize": "Smallest size",
    "case": "Letters",
    "letterSpacing": "Letter spacing",
    "wordGap": "Gap between words",
    "lineHeight": "Line height",
    "emphScale": "Shouted words, size",
    "grow": "Room for a word to grow",
    "tilt": "Most the block turns",
    "paging.mode": "Words arrive",
    "paging.maxWords": "Words at a time",
    "paging.maxLines": "Lines",
    "paging.gapS": "Pause that starts a new phrase",
    "paging.holdS": "Stays after the last word",
    "paging.lingerS": "Last word stays lit",
    "paging.minShowS": "Shortest time on screen",
    "paging.leadS": "Ahead of the sound",
    "paging.overlapS": "Stays into the next phrase",
    "colors.text": "Words",
    "colors.active": "Spoken word",
    "colors.emph": "Shouted words",
    "colors.stroke": "Outline",
    "colors.shadow": "Shadow",
    "colors.plate": "Plate",
    "colors.plateText": "Words on the plate",
    "colors.accent": "Decoration",
    "colors.accent2": "Second decoration",
    "colors.cycle": "Word by word",
}
OPTIONS = {
    "upper": "CAPITALS",
    "lower": "lowercase",
    "title": "Title Case",
    "keep": "as spoken",
    "page": "a phrase at once",
    "build": "as they are spoken",
    "letter": "letter by letter",
    "word": "word by word",
}
UNITS = {"page": "the whole phrase", "word": "word by word", "letter": "letter by letter"}
SLOT_NAMES = {
    "enter": "Arrives",
    "exit": "Leaves",
    "active": "Spoken word",
    "emph": "Shouted word",
    "idle": "While it waits",
}
HEX = re.compile(r"#[0-9a-fA-F]{6}")

# (start, end, word): two phrases, a pause (the exit), a line with shouted words, the exit again
SAMPLE: tuple[tuple[float, float, str], ...] = (
    (0.30, 0.55, "NO"), (0.55, 0.95, "WAY!"),
    (1.05, 1.25, "did"), (1.25, 1.45, "you"), (1.45, 1.70, "see"), (1.70, 2.10, "that?"),
    (3.00, 3.20, "he"), (3.20, 3.45, "just"), (3.45, 3.75, "went"), (3.75, 4.00, "ALL"), (4.00, 4.40, "IN"),
)  # fmt: skip
SAMPLE_S = 5.4
PREVIEW_W, PREVIEW_H, PREVIEW_FPS = 720, 1280, 60
PAD = 60  # clear rows above and below the captions' room in a preview render: overflow lands there
PREVIEW_VERSION = "3"  # part of every preview's name: raise it when the preview itself changes
CLIP_NAME = re.compile(r"[a-z0-9_]{1,40}-[0-9a-f]{20}\.mp4")
_RENDER = threading.Lock()  # one preview is rendered at a time


def previews_root() -> Path:
    env = os.environ.get("CLIPWATCHER_UI_WORK")
    return (Path(env).expanduser() if env else config.ROOT / "work" / "ui") / "captions"


# ------------------------------------------------------------------ the library


def styles() -> tuple[str, ...]:
    """Every caption style a Short can be asked for: the built-in ones, then rm:<template>."""
    return (*[s for s in BUILT_IN if s in CAPTION_STYLES], *[rm.PREFIX + t for t in rm.templates()])


def is_style(style: Any) -> bool:
    return isinstance(style, str) and style in styles()


def _own(name: str) -> dict:
    """A template's parameters as its JSON has them (no config laid over)."""
    try:
        return json.loads((rm.TEMPLATES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"No caption template {name!r}.") from e


def library() -> dict:
    """What the page shows as the caption library."""
    out = [
        {"id": s, "name": name, "about": about, "kind": "built-in", "font": "Anton", "words": None}
        for s, (name, about) in BUILT_IN.items()
        if s in CAPTION_STYLES
    ]
    tuned = rm.settings().get("templates") or {}
    names = rm.templates()
    for t in names:
        try:
            p = _own(t)
        except ValueError:
            continue
        out.append(
            {
                "id": rm.PREFIX + t,
                "name": str(p.get("title") or t.title()),
                "about": str(p.get("description") or ""),
                "kind": "animated",
                "font": str((p.get("font") or {}).get("family") or ""),
                "words": int(p["paging"]["maxWords"]),
                "tuned": bool(tuned.get(t)),
            }
        )
    root, stamp = previews_root(), rm.source_stamp() if names else ""
    for row in out:  # the clips that are made already: the page plays them at once and asks for the rest
        try:
            made = _clip_file(row["id"], None, root, stamp)[0]
        except rm.RemotionError:
            made = None
        row["preview"] = f"/captions/clip/{made.name}" if made and made.is_file() else None
    render = config.global_config().get("render", {})
    why = rm.missing()
    return {
        "styles": out,
        "ready": why is None,
        "missing": why,
        "setup": SETUP,
        "default": str(render.get("captions", "word")),
        "colors": rm.color_mode(),
        "emoji": bool(rm.emoji_family()),
    }


# ------------------------------------------------------------------ what can be set, and within what


_ZOD_LINE = re.compile(r"^\s*(\w+): (z\..+?),?\s*(?://\s*(.*))?$")
_ZOD_NUM = re.compile(r"z\.number\(\)(\.int\(\))?(?:\.min\((-?[\d.]+)\))?(?:\.max\((-?[\d.]+)\))?")
_ZOD_ENUM = re.compile(r"z\.enum\(\[([^\]]*)\]\)")


def _zod(expr: str, about: str | None) -> dict | None:
    """One `key: z....` line of a schema as a field description (numbers and choices; else None)."""
    m = _ZOD_ENUM.match(expr)
    if m:
        return {"kind": "select", "options": re.findall(r'"([^"]+)"', m.group(1)), "help": about or ""}
    m = _ZOD_NUM.match(expr)
    if not m:
        return None
    spec: dict[str, Any] = {"kind": "number", "whole": bool(m.group(1)), "help": about or ""}
    if m.group(2) is not None:
        spec["min"] = float(m.group(2))
    if m.group(3) is not None:
        spec["max"] = float(m.group(3))
    return spec


@functools.lru_cache(maxsize=1)
def _base_specs() -> dict[str, dict]:
    """Limits of the parameters every template has, by path ("paging.maxWords"), from src/core/schema.ts."""
    try:
        src = (rm.REMOTION_DIR / "src" / "core" / "schema.ts").read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, dict] = {}
    section: str | None = None
    opened = {
        "export const colorsSchema": "colors",
        "export const pagingSchema": "paging",
        "export const labelParamsSchema": "label",
        "export const baseParamsShape": "",
        "  timing: z.strictObject({": "timing",
    }
    for line in src.splitlines():
        start = next((v for k, v in opened.items() if line.startswith(k)), None)
        if start is not None:
            section = start
            continue
        if line.startswith("});") or line.startswith("};"):
            section = None
        elif section == "timing" and line.startswith("  }),"):
            section = ""
        if section is None:
            continue
        m = _ZOD_LINE.match(line)
        if m:
            spec = _zod(m.group(2), m.group(3))
            if spec:
                out[f"{section}.{m.group(1)}" if section else m.group(1)] = spec
            elif m.group(2).startswith("z.strictObject({ x:"):  # margin: both on one line
                for k, expr in re.findall(r"(\w): (z\.number\(\)[^,}]*)", m.group(2)):
                    out[f"margin.{k}"] = _zod(expr, m.group(3)) or {}
            continue
        m = re.match(r"^\s*(\w+): hex,\s*(?://\s*(.*))?$", line)  # the colour roles and what each is
        if m and section == "colors":
            out[f"colors.{m.group(1)}"] = {"kind": "color", "help": m.group(2) or ""}
    out["colors.cycle"] = {
        "kind": "colors",
        "help": "per-word colours for templates that rotate them",
        "max": 8,
    }
    return out


@functools.lru_cache(maxsize=64)
def _fx_specs(name: str) -> dict[str, dict]:
    """Limits of a template's own parameters (its `fx` block), from its source."""
    try:
        src = (rm.REMOTION_DIR / "src" / "templates" / f"{name}.tsx").read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.search(r"paramsSchema\(\{(.*?)\n\}\);", src, re.S)
    out: dict[str, dict] = {}
    for line in (m.group(1) if m else "").splitlines():
        mm = _ZOD_LINE.match(line)
        spec = _zod(mm.group(2), mm.group(3)) if mm else None
        if spec:
            out[mm.group(1)] = spec
            continue
        mm = re.match(r'^\s*(\w+): animRef\("(\w+)"\)(\.nullable\(\))?,?\s*(?://\s*(.*))?$', line)
        if mm:
            out[mm.group(1)] = {
                "kind": "motion",
                "slot": mm.group(2),
                "optional": bool(mm.group(3)),
                "help": mm.group(4) or "",
            }
    return out


@functools.lru_cache(maxsize=1)
def animations() -> dict[str, dict]:
    """The animation library: name -> slot, what it does, the units it suits and its own parameters with
    their limits and defaults (remotion/src/core/animations.ts)."""
    try:
        src = (rm.REMOTION_DIR / "src" / "core" / "animations.ts").read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, dict] = {}
    for chunk in src.split("\ndefine({")[1:]:
        name, slot = re.search(r'name: "(\w+)"', chunk), re.search(r'slot: "(\w+)"', chunk)
        shape, defaults = re.search(r"shape: \{([^}]*)\}", chunk), re.search(r"defaults: \{([^}]*)\}", chunk)
        if not (name and slot and shape and defaults):
            continue
        about, units = re.search(r'about:\s*"([^"]*)"', chunk), re.search(r"units: \[([^\]]*)\]", chunk)
        start = {k: float(v) for k, v in re.findall(r"(\w+): (-?[\d.]+)", defaults.group(1))}
        out[name.group(1)] = {
            "slot": slot.group(1),
            "about": about.group(1) if about else "",
            "units": re.findall(r'"(\w+)"', units.group(1)) if units else [],
            "params": {
                k: {"min": float(a), "max": float(b), "default": start.get(k), "label": _label(k)}
                for k, a, b in re.findall(r"(\w+): n\((-?[\d.]+), (-?[\d.]+)\)", shape.group(1))
            },
        }
    return out


def _label(path: str) -> str:
    if path in LABELS:
        return LABELS[path]
    key = path.rsplit(".", 1)[-1]
    if key.lower() == "hz":
        return "Speed (per second)"
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key).lower()
    words = re.sub(r" s$", " (seconds)", words).replace(" hz", " (per second)").replace(" deg", " (degrees)")
    return words[:1].upper() + words[1:]


def _help(note: str) -> str:
    """A schema's own note on a parameter, for the page: the unit alone is spelled out."""
    note = note.strip()
    if note == "em":
        return "In heights of the letters (em)."
    return re.sub(r",? em$", ", in heights of the letters (em)", note)


def _step(spec: dict, value: Any) -> float:
    if spec.get("whole"):
        return 1
    span = spec.get("max", 0) - spec.get("min", 0) if "max" in spec and "min" in spec else abs(value or 1)
    return 1 if span > 100 else 0.5 if span > 30 else 0.1 if span > 6 else 0.01


def _field(path: str, spec: dict, value: Any, own: Any) -> dict:
    f = {"path": path, "label": _label(path), "kind": spec.get("kind", "number"), "value": value, "own": own}
    f["help"] = _help(spec.get("help", ""))
    if f["kind"] == "number":
        f.update({k: spec[k] for k in ("min", "max") if k in spec}, whole=bool(spec.get("whole")))
        if "max" not in f:  # no upper limit in the schema: the slider still needs an end
            top = max(abs(float(own or 0)) * 3, float(f.get("min", 0)) + 1)
            f["soft_max"] = round(top, 2)
        f["step"] = _step({**spec, "max": f.get("max", f.get("soft_max"))}, own)
    elif f["kind"] == "select":
        f["options"] = [{"id": o, "name": OPTIONS.get(o, o)} for o in spec["options"]]
    elif f["kind"] == "colors":
        f["max"] = spec.get("max", 8)
    return f


def _specs(name: str) -> dict[str, dict]:
    """Every parameter of a template the page may set, by path, in the order of its JSON."""
    own = _own(name)
    base, fx = _base_specs(), _fx_specs(name)
    out: dict[str, dict] = {}
    for key, value in own.items():
        if key in HIDDEN:
            continue
        if isinstance(value, dict):
            for k in value:
                spec = fx.get(k) if key == "fx" else base.get(f"{key}.{k}")
                if spec:
                    out[f"{key}.{k}"] = spec
        elif key in base:
            out[key] = base[key]
    return out


def _get(params: dict, path: str) -> Any:
    cur: Any = params
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def fields(style: str) -> dict:
    """The parameters of an animated template for the page: groups of fields with the value in force
    (the template's own, the config's laid over it), the template's own value, and the limits."""
    if not rm.is_rm(style) or rm.template_name(style) not in rm.templates():
        raise ValueError("Only the animated templates have settings of their own.")
    name = rm.template_name(style)
    own, now = _own(name), rm.load_template(name)
    specs = _specs(name)
    lib = animations()
    groups = []
    for gid, title, keys in GROUPS:
        rows = []
        for path, spec in specs.items():
            motion = spec.get("kind") == "motion"
            if (gid == "motion") != motion or (
                not motion and path.split(".")[0] not in keys and path not in keys
            ):
                continue
            f = _field(path, spec, _get(now, path), _get(own, path))
            if motion:
                f["label"] = SLOT_NAMES.get(spec["slot"], spec["slot"])
                f["optional"] = spec["optional"]
                f["choices"] = [
                    {"id": n, "about": a["about"], "units": a["units"], "params": a["params"]}
                    for n, a in lib.items()
                    if a["slot"] == spec["slot"]
                ]
            rows.append(f)
        if rows:
            groups.append({"id": gid, "title": title, "fields": rows})
    return {
        "style": style,
        "name": str(own.get("title") or name),
        "about": str(own.get("description") or ""),
        "font": str((own.get("font") or {}).get("family") or ""),
        "tuned": bool((rm.settings().get("templates") or {}).get(name)),
        "units": UNITS,
        "groups": groups,
    }


def _number(path: str, spec: dict, v: Any) -> float | int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) == float("inf"):
        raise ValueError(f"{_label(path)}: a number.")
    if spec.get("whole") and float(v) != int(v):
        raise ValueError(f"{_label(path)}: a whole number.")
    lo, hi = spec.get("min"), spec.get("max")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        ends = f"between {lo:g} and {hi:g}" if lo is not None and hi is not None else f"at least {lo:g}"
        raise ValueError(f"{_label(path)}: {ends}.")
    return int(v) if spec.get("whole") else float(v)


def _motion(path: str, spec: dict, v: Any) -> dict | None:
    lib = animations()
    if v is None:
        if not spec.get("optional"):
            raise ValueError(f"{SLOT_NAMES.get(spec['slot'], spec['slot'])}: pick an animation.")
        return None
    name = v.get("name") if isinstance(v, dict) else None
    a = lib.get(name) if isinstance(name, str) else None
    if not a or a["slot"] != spec["slot"]:
        have = ", ".join(n for n, x in lib.items() if x["slot"] == spec["slot"])
        raise ValueError(f"{SLOT_NAMES.get(spec['slot'], spec['slot'])}: one of {have}.")
    out: dict[str, Any] = {"name": name, "params": {}}
    for k, val in (v.get("params") or {}).items():
        if k not in a["params"]:
            raise ValueError(f"{name} has no setting {k!r}.")
        out["params"][k] = _number(f"{path}.{k}", a["params"][k], val)
    if spec["slot"] in ("enter", "exit"):
        by = v.get("by") or "page"
        if by not in a["units"]:
            suits = " or ".join(UNITS.get(u, u) for u in a["units"])
            raise ValueError(f"{name} moves {suits}, not {UNITS.get(by, by)}.")
        out["by"] = by
    return out


def check(style: str, values: dict | None) -> dict:
    """Settings from the page ({"size": 80, "paging.maxWords": 2, "fx.enter": {...}}) as overrides for the
    template ({"size": 80, "paging": {"maxWords": 2}, ...}), every value inside the template's own limits
    and the colours still readable on each other. ValueError says, in words for the page, what is wrong."""
    if not values:
        return {}
    if not rm.is_rm(style) or rm.template_name(style) not in rm.templates():
        raise ValueError("Only the animated templates have settings of their own.")
    if not isinstance(values, dict) or len(values) > 200:
        raise ValueError("Settings: a list of values by name.")
    name = rm.template_name(style)
    specs = _specs(name)
    out: dict[str, Any] = {}
    for path, v in values.items():
        spec = specs.get(path)
        if spec is None:
            raise ValueError(f"{style} has no setting {str(path)[:60]!r}.")
        kind = spec.get("kind", "number")
        if kind == "number":
            v = _number(path, spec, v)
        elif kind == "select":
            if v not in spec["options"]:
                raise ValueError(f"{_label(path)}: one of {', '.join(spec['options'])}.")
        elif kind == "color":
            if not isinstance(v, str) or not HEX.fullmatch(v):
                raise ValueError(f"{_label(path)}: a colour like #FFE600.")
            v = v.upper()
        elif kind == "colors":
            ok = isinstance(v, list) and 1 <= len(v) <= spec.get("max", 8)
            if not ok or not all(isinstance(c, str) and HEX.fullmatch(c) for c in v):
                raise ValueError(f"{_label(path)}: one to {spec.get('max', 8)} colours like #FFE600.")
            v = [c.upper() for c in v]
        elif kind == "motion":
            v = _motion(path, spec, v)
        cur = out
        *parents, leaf = path.split(".")
        for part in parents:
            cur = cur.setdefault(part, {})
        cur[leaf] = v
    if "colors" in out:
        _readable(name, out)
    return out


def _readable(name: str, overrides: dict) -> None:
    """The contrast floor between the colours that sit next to each other (D19), for colours set by hand."""
    p = rm.load_template(name, overrides)
    for a, b in p["auto"].get("pairs") or []:
        ones = p["colors"].get(a), p["colors"].get(b)
        if not all(isinstance(c, str) for c in ones):
            continue
        ratio = palette.contrast(*ones)
        if ratio < palette.FLOOR:
            raise ValueError(
                f"{_label('colors.' + a)} on {_label('colors.' + b).lower()} is {ratio:.1f}:1. "
                f"Captions keep at least {palette.FLOOR:g}:1 so they can be read on any video."
            )


# ------------------------------------------------------------------ a clip that shows a style


def _backdrop(root: Path) -> Path:
    """A still that stands in for a Short: a webcam pane over a gameplay pane, the seam at half height."""
    out = root / f"backdrop-{PREVIEW_VERSION}.png"
    if out.is_file():
        return out
    from PIL import Image, ImageDraw, ImageFilter

    w, h = rm.FRAME_W, rm.FRAME_H
    img = Image.new("RGB", (w, h))
    px = ImageDraw.Draw(img)
    for y in range(h):  # two panes, each its own run of colour
        k = (y % (h // 2)) / (h // 2)
        top, low = ((58, 47, 96), (118, 62, 110)) if y < h // 2 else ((18, 54, 78), (30, 96, 92))
        px.line([(0, y), (w, y)], fill=tuple(round(a + (b - a) * k) for a, b in zip(top, low, strict=True)))
    soft = Image.new("RGB", (w, h))
    soft.paste(img)
    blobs = ImageDraw.Draw(soft)
    blobs.ellipse((330, 250, 750, 760), fill=(214, 150, 132))  # someone at the camera
    blobs.rectangle((80, 1150, 520, 1500), fill=(40, 132, 118))  # something going on in the game
    blobs.rectangle((640, 1320, 1010, 1780), fill=(22, 78, 104))
    blobs.ellipse((700, 1020, 980, 1240), fill=(238, 196, 96))
    img = Image.blend(img, soft.filter(ImageFilter.GaussianBlur(70)), 0.75)
    root.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.png")
    img.save(tmp)
    os.replace(tmp, out)
    return out


def _sample_words() -> list[Word]:
    return [Word(s, e, t) for s, e, t in SAMPLE]


def _clip_file(
    style: str, over: dict | None, root: Path, stamp: str | None = None
) -> tuple[Path, dict | None]:
    """Where the preview of a style with these overrides is kept, and the template's parameters with
    them. The name holds everything the clip depends on: the template as it is now (its sources, the
    config's version of it), the overrides, the sample."""
    if not rm.is_rm(style):
        key = json.dumps([PREVIEW_VERSION, style, SAMPLE])
        return root / f"{style}-{hashlib.sha256(key.encode()).hexdigest()[:20]}.mp4", None
    name = rm.template_name(style)
    params = rm.load_template(name, over)
    blob = [PREVIEW_VERSION, style, params, rm.source_stamp() if stamp is None else stamp, SAMPLE]
    key = hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:20]
    return root / f"{name.lower()}-{key}.mp4", params


def _leaves_box(ov: rm.Overlay, box: rm.Box, pad: int) -> bool:
    """Whether anything is drawn outside the room the captions have, in a render with `pad` clear rows
    above and below it (the frame checks' own rule: every drawn pixel inside the box)."""
    from PIL import Image

    seen: set[int] = set()  # blank frames are links to one file
    for f in sorted(ov.dir.glob("*.png")):
        ino = f.stat().st_ino
        if ino in seen:
            continue
        seen.add(ino)
        with Image.open(f) as im:
            ink = im.convert("RGBA").getchannel("A").getbbox()
        if ink and (ink[0] < box.x0 or ink[2] > box.x1 or ink[1] < pad or ink[3] > pad + box.h):
            return True
    return False


def preview(style: str, values: dict | None = None, root: Path | None = None) -> dict:
    """A short clip of the style over a stand-in Short: {"file", "warnings"}. Made once per style and set
    of values, then kept. ValueError says, in words for the page, why there is none."""
    if not is_style(style):
        raise ValueError("Pick a caption style.")
    over = check(style, values) if values else {}
    root = Path(root) if root else previews_root()
    name = rm.template_name(style) if rm.is_rm(style) else style
    out, params = _clip_file(style, over, root)
    note = out.with_suffix(".json")
    if out.is_file():
        try:
            return {"file": out, "warnings": json.loads(note.read_text(encoding="utf-8"))["warnings"]}
        except (OSError, ValueError, KeyError):
            return {"file": out, "warnings": []}
    if rm.is_rm(style) and rm.missing():
        raise ValueError(f"The animated templates need their one-time setup on this machine ({SETUP}).")
    root.mkdir(parents=True, exist_ok=True)
    with _RENDER:
        if not out.is_file():
            warnings = _make(style, name, params, _backdrop(root), out)
            note.write_text(json.dumps({"style": style, "warnings": warnings}), encoding="utf-8")
    return {"file": out, "warnings": json.loads(note.read_text(encoding="utf-8"))["warnings"]}


def _make(style: str, name: str, params: dict | None, backdrop: Path, out: Path) -> list[str]:
    from clipwatcher.render.branding import FONTS_DIR
    from clipwatcher.render.template import SEAM_Y, esc_filter_path

    warnings: list[str] = []
    words = _sample_words()
    with tempfile.TemporaryDirectory(prefix="clipwatcher_cap_") as td:
        work = Path(td)
        cmd = [config.ffmpeg_bin(), "-y", "-v", "error", "-loop", "1", "-framerate", str(PREVIEW_FPS)]
        cmd += ["-i", str(backdrop)]
        if rm.is_rm(style):
            box = rm.caption_box(SEAM_Y, "seam")
            try:
                props, plan = rm.build_props(
                    words, SAMPLE_S, name, box=box, fps=PREVIEW_FPS, colors="fixed", params=params, pad=PAD
                )
                if plan.empty:
                    raise ValueError("Nothing to show with these settings.")
                tabs = max(2, (os.cpu_count() or 4) // 2)  # the machine may be recording as well
                ov = rm.render_props(props, work / "rm", y=box.y0 - PAD, concurrency=tabs, timeout_s=120)
            except rm.RemotionError as e:
                raise ValueError(f"The template refused these settings: {_reason(e)}") from e
            if _leaves_box(ov, box, PAD):
                warnings.append("With these settings the words leave the room they have in this sample.")
            cmd += rm.overlay_input(ov)
            graph = "[0:v]format=yuv420p[base];" + rm.overlay_filter(1, ov, "[base]", "[full]")
        else:
            from clipwatcher.render.captions import write_ass

            ass = write_ass(work / "subs.ass", words, SAMPLE_S, credit=None, style=style, y=SEAM_Y)
            graph = f"[0:v]ass=filename={esc_filter_path(ass)}:fontsdir={esc_filter_path(FONTS_DIR)}[full]"
        graph += f";[full]scale={PREVIEW_W}:{PREVIEW_H}:flags=lanczos,format=yuv420p[v]"
        tmp = out.with_suffix(".tmp.mp4")
        cmd += ["-filter_complex", graph, "-map", "[v]", "-t", f"{SAMPLE_S:g}", "-r", str(PREVIEW_FPS)]
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "27",
            "-an",
            "-movflags",
            "+faststart",
            str(tmp),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as e:
            raise ValueError(f"The preview could not be made: {e}") from e
        if r.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            raise ValueError(f"The preview could not be made: {r.stderr[-300:]}")
        os.replace(tmp, out)
    return warnings


def _reason(e: Exception) -> str:
    """The part of a renderer's refusal a person can act on (its last line, without the stack)."""
    lines = [ln.strip() for ln in str(e).splitlines() if ln.strip() and not ln.strip().startswith("at ")]
    return (lines[-1] if lines else str(e))[-300:]


def clip_path(file: str, root: Path | None = None) -> Path | None:
    """A preview clip by its file name, or None: only names this module gives out are looked up."""
    if not CLIP_NAME.fullmatch(file or ""):
        return None
    p = (Path(root) if root else previews_root()) / file
    return p if p.is_file() else None


def warm(root: Path | None = None) -> int:
    """Make the preview of every style that has none yet (the app does this in the background when it
    starts, so the library plays at once). Returns how many are there."""
    n = 0
    for s in styles():
        try:
            preview(s, None, root)
            n += 1
        except ValueError as e:
            log.info("no preview of %s: %s", s, e)
            if rm.is_rm(s) and rm.missing():
                break
    return n


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["warm"]:
        print(__doc__)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"{warm()} of {len(styles())} previews in {previews_root()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
