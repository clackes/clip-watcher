"""The Library place of the companion app: the requests of /api/library/... (see server.PLACES).

The Library is where the caption templates are looked after: every style a Short can get, this machine's
edit of a template that ships, and templates of the owner's own. An own template is a shipped template
under a name of its own with its own settings; it is kept in config/local.yaml (render.rm.presets, see
render/remotion.presets) and is a caption style like any other, `rm:<id>`: the lanes, a clip rendered
again and the previews all take it.

    GET  /api/library                 every style, with what the page marks them by
    GET  /api/library/fields?style=   what can be set on an animated template
    POST /api/library/preview         {style, settings, whole}       a clip that shows it
    POST /api/library/create          {from, name, settings}         a template of your own
    POST /api/library/save            {style, settings}              keep its settings
    POST /api/library/rename          {style, name}
    POST /api/library/delete          {style}
    POST /api/library/reset           {style}                        a shipped one as it came
    POST /api/library/use             {style, colors}                the style of new clips

Settings are always the whole of what differs from the template as it shipped ({"size": 80,
"colors.active": "#00E5FF"}), checked by ui/captions.check before anything is written.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from typing import Any

from clipwatcher import config
from clipwatcher.render import remotion as rm
from clipwatcher.ui import NotFound, captions, settings

NAME_MAX = 40  # characters of a template's name
OWN_MAX = 60  # templates of one's own the library holds
_WRITE = threading.Lock()  # config/local.yaml is read, changed and written by one request at a time


# ------------------------------------------------------------------ names and ids


def _taken_names(but: str | None = None) -> set[str]:
    """Names a template cannot take: the built-in styles', the shipped templates', the other own ones'."""
    names = {name for name, _ in captions.BUILT_IN.values()}
    for t in rm.templates():
        try:
            names.add(str(captions._own(t).get("title") or t))
        except ValueError:
            continue
    names.update(p["name"] for pid, p in rm.presets().items() if pid != but)
    return {n.casefold() for n in names}


def clean_name(name: Any, but: str | None = None) -> str:
    """A template's name as it is kept: plain text on one line, trimmed, NAME_MAX characters at most and
    not the name of another template (`but`: the id of the one being renamed). ValueError otherwise."""
    if not isinstance(name, str):
        raise ValueError("Give your template a name.")
    name = " ".join("".join(ch if ch.isprintable() else " " for ch in name).split())
    if not name:
        raise ValueError("Give your template a name.")
    if len(name) > NAME_MAX:
        raise ValueError(f"A name can be up to {NAME_MAX} characters long.")
    if name.casefold() in _taken_names(but):
        raise ValueError(f"There is already a template called {name}. Pick another name.")
    return name


def new_id(name: str) -> str:
    """An id for a new own template, made from its name: [a-z0-9_]{1,40}, never a shipped template's
    name nor an id that is in use (a number is added then)."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", plain).strip("_")[:40].strip("_") or "mine"
    held = rm.settings().get("presets")
    taken = {*rm.templates(), *captions.BUILT_IN, *(held if isinstance(held, dict) else {})}
    pid, n = slug, 1
    while pid in taken:
        n += 1
        pid = slug[: 40 - len(f"_{n}")].rstrip("_") + f"_{n}"
    return pid


def _own_only(style: Any, doing: str) -> tuple[str, dict]:
    """(id, the own template) of a style that is one of this machine's own; ValueError otherwise."""
    try:
        name, _, own = captions.resolve(style)
    except ValueError:
        raise NotFound("This template is not in the library any more.") from None
    if own is None:
        raise ValueError(f"Only a template of your own can be {doing}.")
    return name, own


def _row(style: str) -> dict:
    return next(r for r in captions.library()["styles"] if r["id"] == style)


# ------------------------------------------------------------------ what the page asks for


def state() -> dict:
    """The library as the page shows it."""
    return {**captions.library(), "name_max": NAME_MAX, "own_max": OWN_MAX}


def create(source: Any, name: Any, values: dict | None = None) -> dict:
    """A new template of one's own, made from any animated style: the shipped template that draws it,
    under `name`, with `values` (everything that differs from that template as it shipped; None: what
    the source holds, when it is an own template). Returns its row of the library."""
    try:
        _, base, origin = captions.resolve(source)
    except ValueError:
        raise ValueError("Start from one of the animated templates.") from None
    name = clean_name(name)
    if len(rm.presets()) >= OWN_MAX:
        raise ValueError(f"The library holds up to {OWN_MAX} templates of your own. Delete one first.")
    if values is None:
        over = dict(origin["settings"]) if origin else {}
    else:
        over = captions.check(rm.PREFIX + base, values, whole=True)
    pid = new_id(name)
    settings.save_preset(pid, {"name": name, "base": base, "settings": over, "created": int(time.time())})
    return _row(rm.PREFIX + pid)


def save(style: Any, values: dict | None) -> dict:
    """Keep `values` (everything that differs from the template as it shipped) as the settings of an own
    template, or as this machine's edit of a shipped one. Returns its fields as they are now."""
    try:
        name, base, own = captions.resolve(style)
    except ValueError as e:
        if isinstance(style, str) and captions.is_style(style):
            raise ValueError("The built-in styles come as they are: there is nothing to set.") from e
        raise NotFound("This template is not in the library any more.") from e
    over = captions.check(style, values or {}, whole=True)
    if own is None:
        settings.save_template(name, over)
    else:
        kept = (rm.settings().get("presets") or {}).get(name) or {}
        settings.save_preset(name, {**kept, "name": own["name"], "base": base, "settings": over})
    return captions.fields(style)


def rename(style: Any, name: Any) -> dict:
    """Another name for an own template. Its style (rm:<id>) stays, so clips and settings that name it
    still find it."""
    pid, own = _own_only(style, "renamed")
    name = clean_name(name, but=pid)
    kept = (rm.settings().get("presets") or {}).get(pid) or {}
    settings.save_preset(pid, {**kept, "name": name, "base": own["base"], "settings": own["settings"]})
    return _row(rm.PREFIX + pid)


def delete(style: Any) -> dict:
    """Take an own template out of the library. When it is the style of new clips, they go back to the
    template it was made from. Returns {"deleted", "default", "moved"}."""
    pid, own = _own_only(style, "deleted")
    moved = str(config.global_config().get("render", {}).get("captions", "")) == rm.PREFIX + pid
    if moved:
        settings.save({"render.captions": rm.PREFIX + own["base"]}, merge=True)
    settings.save_preset(pid, None)
    return {"deleted": rm.PREFIX + pid, "default": captions.library()["default"], "moved": moved}


def reset(style: Any) -> dict:
    """A shipped template back to how it came: this machine's edit of it is dropped."""
    try:
        name, _, own = captions.resolve(style)
    except ValueError:
        raise ValueError("Only a template that was edited here can go back to how it came.") from None
    if own is not None:
        raise ValueError("A template of your own has no earlier version to go back to.")
    settings.save_template(name, {})
    return captions.fields(style)


def use(style: Any, colors: Any = None) -> dict:
    """Make `style` the caption style of new clips and, for an animated one, set how its colours are
    picked (auto: for each video; fixed: the template's own)."""
    if not captions.is_style(style):
        raise NotFound("This template is not in the library any more.")
    values: dict[str, Any] = {"render.captions": style}
    if rm.is_rm(style) and colors is not None:
        if colors not in captions.COLOUR_MODES:
            raise ValueError("Colours: picked for each video, or the template's own.")
        values["render.rm.colors"] = colors
    settings.save(values, merge=True)
    lib = captions.library()
    return {"default": lib["default"], "colors": lib["colors"]}


# ------------------------------------------------------------------ the requests


def get(app: Any, parts: list[str], query: dict[str, list[str]]) -> Any:
    if not parts:
        return state()
    if parts == ["fields"]:  # ?style=rm:punch
        return captions.fields((query.get("style") or [""])[0])
    raise NotFound("Not found.")


def post(app: Any, parts: list[str], body: dict[str, Any]) -> Any:
    what = parts[0] if len(parts) == 1 else ""
    style, values = body.get("style"), body.get("settings")
    if values is not None and not isinstance(values, dict):
        raise ValueError("Settings: a list of values by name.")
    if what == "preview":  # made once per look, then kept; it waits for its turn, not for the writes
        made = captions.preview(str(style or ""), values or None, whole=body.get("whole") is True)
        return {"url": f"/captions/clip/{made['file'].name}", "warnings": made["warnings"]}
    with _WRITE:
        if what == "create":
            return create(body.get("from"), body.get("name"), values)
        if what == "save":
            return save(style, values)
        if what == "rename":
            return rename(style, body.get("name"))
        if what == "delete":
            return delete(style)
        if what == "reset":
            return reset(style)
        if what == "use":
            return use(style, body.get("colors"))
    raise NotFound("Not found.")
