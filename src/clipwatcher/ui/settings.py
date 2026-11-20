"""The settings the companion app shows and may change: a chosen few from config/global.yaml, each with its
range. Changes are written to config/local.yaml (this machine only), never to global.yaml."""

from __future__ import annotations

from typing import Any

from clipwatcher import config


def _f(key: str, label: str, kind: str, hint: str = "", **more: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "type": kind, "hint": hint, **more}


def _r(
    key: str, label: str, lo: float, hi: float, step: float, hint: str = "", unit: str = ""
) -> dict[str, Any]:
    return _f(key, label, "range", hint, min=lo, max=hi, step=step, unit=unit)


GROUPS: list[dict[str, Any]] = [
    {
        "title": "Detector",
        "about": "How a chat spike becomes a clip",
        "fields": [
            _r(
                "detector.weights.U",
                "Weight: chatters",
                0,
                1,
                0.05,
                "Different people typing in ten seconds.",
            ),
            _r("detector.weights.M", "Weight: messages", 0, 1, 0.05, "Raw message volume."),
            _r("detector.weights.E", "Weight: keywords", 0, 1, 0.05, "Laughter and reaction words."),
            _r(
                "detector.weights.S",
                "Weight: short messages",
                0,
                1,
                0.05,
                "Share of short or repeated messages.",
            ),
            _r(
                "detector.fire_q",
                "A spike fires in the top",
                0.9,
                0.995,
                0.005,
                "Share of the stream's seconds it must beat.",
                "top",
            ),
            _r(
                "detector.keep_q",
                "It is clipped in the top",
                0.95,
                0.999,
                0.001,
                "Its peak must reach this.",
                "top",
            ),
            _r(
                "detector.max_per_hour",
                "Clips an hour, at most",
                1,
                20,
                1,
                "Past this a spike must beat the hour's typical one.",
            ),
            _r(
                "detector.cooldown_s",
                "Pause between clips",
                0,
                180,
                5,
                "A spike 1.5 times the last one overrides it.",
                "s",
            ),
        ],
    },
    {
        "title": "Clip and render",
        "about": "What comes out",
        "fields": [
            _r("detector.before_s", "Footage before the moment", 10, 60, 1, "", "s"),
            _r(
                "detector.after_s",
                "Footage after the moment",
                5,
                30,
                1,
                "Together, what the judge may trim.",
                "s",
            ),
            _r("render.clip_min_s", "Shortest clip", 12, 45, 1, "", "s"),
            _r("render.clip_max_s", "Longest clip", 30, 59, 1, "", "s"),
            _f(
                "render.layout",
                "Layout",
                "select",
                "split: webcam over gameplay.",
                options=["split", "stack"],
            ),
            _f("render.captions", "Caption template", "select", options=["word", "pop", "classic", "bounce"]),
            _f(
                "render.jump_cuts",
                "Cut pauses without speech",
                "toggle",
                "Pauses of 0.8 s and longer are removed.",
            ),
            _f("render.sfx", "Meme sound on judge beats", "toggle"),
            _f(
                "render.profile",
                "Encoder profile",
                "select",
                options=["cpu_batch", "cpu_live", "nvenc", "mac_preview"],
            ),
        ],
    },
    {
        "title": "Judge, chat and publishing",
        "about": "The parts around the detector",
        "fields": [
            _f(
                "judge.mode",
                "Judge mode",
                "select",
                "two_stage screens with the small model first.",
                options=["single", "two_stage", "off"],
            ),
            _f("judge.effort", "Judge effort", "select", options=["low", "medium", "high"]),
            _r("judge.frames_max", "Frames per clip, at most", 8, 24, 1, "One frame every 2.5 seconds."),
            _r(
                "buffer.window_min",
                "Video kept in the rolling window",
                15,
                60,
                5,
                "The chat of a broadcast is kept whole.",
                "min",
            ),
            _r("chat.backfill_max_h", "Chat read back on joining", 1, 24, 1, "", "h"),
            _r("kick.max_rps", "Requests a second to Kick", 0.5, 1.6, 0.1, "Kick blocks beyond about 1.6."),
            _f(
                "publish.auto_approve",
                "Approve top clips without a person",
                "toggle",
                "Off: a person approves every clip in Telegram.",
            ),
            _f("publish.youtube_mode", "YouTube upload through", "select", options=["browser", "api"]),
        ],
    },
]
FIELDS: dict[str, dict[str, Any]] = {f["key"]: f for g in GROUPS for f in g["fields"]}


def _get(tree: dict[str, Any], key: str) -> Any:
    node: Any = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _put(tree: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    for part in parts[:-1]:
        tree = tree.setdefault(part, {})
    tree[parts[-1]] = value


def _clean(field: dict[str, Any], value: Any) -> Any:
    """``value`` as this field may hold it; ValueError otherwise."""
    kind = field["type"]
    if kind == "toggle":
        if not isinstance(value, bool):
            raise ValueError(f"{field['key']}: on or off")
        return value
    if kind == "select":
        if value not in field["options"]:
            raise ValueError(f"{field['key']}: one of {', '.join(field['options'])}")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field['key']}: a number")
    if not field["min"] <= value <= field["max"]:
        raise ValueError(f"{field['key']}: between {field['min']} and {field['max']}")
    whole = float(field["step"]).is_integer() and float(field["min"]).is_integer()
    return int(round(value)) if whole else round(float(value), 4)


def schema() -> list[dict[str, Any]]:
    """The groups with, for every field, the value in use and the one global.yaml holds."""
    base, now = config._load("global.yaml"), config.global_config()
    out = []
    for g in GROUPS:
        fields = []
        for f in g["fields"]:
            default, value = _get(base, f["key"]), _get(now, f["key"])
            fields.append({**f, "default": default, "value": default if value is None else value})
        out.append({"title": g["title"], "about": g["about"], "fields": fields})
    return out


def save(values: dict[str, Any], merge: bool = False) -> dict[str, Any]:
    """Keep ``values`` (key -> value) as this machine's settings: only the ones that differ from global.yaml
    are written to config/local.yaml. Unknown keys and values out of range raise ValueError; nothing is
    written then. ``merge`` changes just the keys given and leaves the page's other fields as they are;
    otherwise a field that is not given goes back to global.yaml. Returns what was written (key -> value)."""
    base = config._load("global.yaml")
    was = _flat(config.local_overrides())
    if merge:
        values = {**{k: v for k, v in was.items() if k in FIELDS}, **values}
    tree: dict[str, Any] = {}
    kept: dict[str, Any] = {}
    for key, value in values.items():
        if key not in FIELDS:
            raise ValueError(f"{key}: not a setting this page may change")
        value = _clean(FIELDS[key], value)
        if value != _get(base, key):
            _put(tree, key, value)
            kept[key] = value
    # what the file held outside the fields of this page stays (someone may have written it by hand)
    for key, value in was.items():
        if key not in FIELDS:
            _put(tree, key, value)
    config.save_local(tree)
    return kept


def _flat(tree: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in tree.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and key not in FIELDS:
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out
