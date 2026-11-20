"""The Library of the companion app: templates of one's own beside the ones that ship, kept in this
machine's config, checked before they are written, and a caption style like any other from then on. No
Node is needed: the renderer is stood in for."""

import http.client
import json
import threading

import pytest
import yaml

from clipwatcher import config
from clipwatcher.render import remotion as rm
from clipwatcher.ui import NotFound, captions, library, settings
from clipwatcher.ui.runs import RunManager, command
from clipwatcher.ui.server import App, make_server


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    """This machine's config in a folder of the test's own, local.yaml counted."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "global.yaml").write_text("render:\n  captions: word\n  rm:\n    colors: auto\n")
    monkeypatch.setenv("CLIPWATCHER_LOCAL", "1")
    config.global_config.cache_clear()
    yield tmp_path / "config"
    config.global_config.cache_clear()


@pytest.fixture
def made(monkeypatch):
    """Previews without a renderer: what would be rendered is noted, a small file stands for the clip."""
    calls = []

    def make(style, name, params, backdrop, out):
        calls.append((style, name, params))
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(40))
        return []

    monkeypatch.setattr(captions, "_make", make)
    monkeypatch.setattr(captions, "_backdrop", lambda root: root / "backdrop.png")
    monkeypatch.setattr(rm, "missing", lambda: None)
    return calls


def _local(cfgdir):
    return yaml.safe_load((cfgdir / "local.yaml").read_text())


def _by_id(style):
    return next(s for s in captions.library()["styles"] if s["id"] == style)


# ------------------------------------------------------------------ a template of one's own


def test_an_own_template_is_a_named_copy_kept_in_this_machines_config(cfgdir):
    row = library.create("rm:punch", "  Neon   night ", {"size": 120, "colors.active": "#00e5ff"})
    assert row["id"] == "rm:neon_night" and row["name"] == "Neon night" and row["own"] is True
    assert row["base"] == "rm:punch" and row["base_name"] == "Punch" and row["kind"] == "animated"
    # its colours were set by hand (the page then keeps them on every video); the shipped one's were not
    assert row["colored"] is True and _by_id("rm:punch")["colored"] is False and row["tuned"] is False
    assert library.create("rm:punch", "Plain", {"size": 60})["colored"] is False
    library.delete("rm:plain")
    kept = _local(cfgdir)["render"]["rm"]["presets"]["neon_night"]
    assert kept["name"] == "Neon night" and kept["base"] == "punch" and kept["created"] > 0
    assert kept["settings"] == {"size": 120, "colors": {"active": "#00E5FF"}}
    # it is a caption style like any other: listed, accepted by a render command, offered in Settings
    assert "rm:neon_night" in captions.styles() and captions.is_style("rm:neon_night")
    assert captions.styles().index("rm:neon_night") == len(captions.styles()) - 1  # after the shipped
    assert len(captions.library()["styles"]) == 30 and rm.templates().count("neon_night") == 0
    cmd = command("render", {"clip": "zack-live-1", "captions": "rm:neon_night", "settings": {"size": 90}})
    assert cmd[cmd.index("--captions") + 1] == "rm:neon_night"
    assert "rm:neon_night" in settings._options(settings.FIELDS["render.captions"])
    # what it is drawn with: the base's parameters with its own laid over
    p = rm.load_template("neon_night")
    assert p["size"] == 120 and p["colors"]["active"] == "#00E5FF" and p["title"] == "Punch"
    assert rm.base_name("neon_night") == "punch" and rm.load_template("punch")["size"] == 92


def test_ids_come_from_the_name_and_never_take_a_shipped_templates(cfgdir):
    assert library.create("rm:punch", "Neon!")["id"] == "rm:neon_2"  # neon ships
    assert library.create("rm:punch", "NEON?")["id"] == "rm:neon_3"
    assert library.create("rm:jelly", "Été — très fort")["id"] == "rm:ete_tres_fort"
    assert library.create("rm:jelly", "🔥🔥")["id"] == "rm:mine"
    long = library.create("rm:jelly", "a" * 40)
    assert long["id"] == "rm:" + "a" * 40 and rm.PRESET_ID.fullmatch(long["id"][3:])
    assert all(rm.PRESET_ID.fullmatch(p) and p not in rm.templates() for p in rm.presets())
    for bad, why in (
        ("", "Give your template a name"),
        ("   \n\t ", "Give your template a name"),
        (None, "Give your template a name"),
        ("x" * 41, "up to 40 characters"),
        ("punch", "already a template called punch"),  # a shipped template's
        ("neon!", "already a template called neon!"),  # another own one's, whatever the case
        ("Word", "already a template called Word"),  # a built-in style's
    ):
        with pytest.raises(ValueError, match=why):
            library.create("rm:punch", bad)
    with pytest.raises(ValueError, match="Start from one of the animated"):
        library.create("word", "Mine")
    with pytest.raises(ValueError, match="Start from one of the animated"):
        library.create("rm:nope", "Mine")
    assert len(rm.presets()) == 5


def test_an_own_template_stands_on_the_base_as_it_shipped(cfgdir):
    settings.save_template("punch", {"size": 100, "colors": {"text": "#101010", "stroke": "#FFFFFF"}})
    assert rm.load_template("punch")["size"] == 100
    mine = library.create("rm:punch", "Mine", {"lineHeight": 1.3})["id"]
    p = rm.load_template("mine")
    assert p["size"] == 92 and p["lineHeight"] == 1.3 and p["colors"]["text"] == "#FFFFFF"
    # its fields: what it holds now beside the base as it shipped
    f = {x["path"]: x for g in captions.fields(mine)["groups"] for x in g["fields"]}
    assert f["lineHeight"]["value"] == 1.3 and f["lineHeight"]["own"] == 1.1 and f["size"]["value"] == 92
    info = captions.fields(mine)
    assert info["name"] == "Mine" and info["own"] is True and info["base"] == "rm:punch"
    assert info["base_name"] == "Punch" and info["tuned"] is False
    assert captions.fields("rm:punch")["tuned"] is True and captions.fields("rm:punch")["own"] is False
    # the whole of a template's settings is read on the shipped colours, a change on the ones in force
    yellow = {"colors.active": "#FFE600"}  # fine on the shipped black outline, not on this machine's white
    assert captions.check("rm:punch", yellow, whole=True) == {"colors": {"active": "#FFE600"}}
    with pytest.raises(ValueError, match="at least 4.5:1"):
        captions.check("rm:punch", yellow)
    assert captions.check(mine, yellow) == {"colors": {"active": "#FFE600"}}
    # a copy of an own template takes what it holds, and stands on the same base
    twin = library.create(mine, "Twin")
    assert twin["base"] == "rm:punch" and rm.load_template("twin")["lineHeight"] == 1.3
    other = library.create(mine, "Other", {"size": 70})
    assert rm.load_template(other["id"][3:])["lineHeight"] == 1.1  # the settings given are the whole


def test_everything_is_checked_before_it_is_written(cfgdir):
    for bad, why in (
        ({"size": 2}, "at least 10"),
        ({"nope": 1}, "no setting"),
        ({"colors.active": "#111111"}, "at least 4.5:1"),
        ({"colors.text": "red"}, "a colour like"),
    ):
        with pytest.raises(ValueError, match=why):
            library.create("rm:punch", "Mine", bad)
    assert not (cfgdir / "local.yaml").exists() and rm.presets() == {}
    mine = library.create("rm:punch", "Mine", {"size": 80})["id"]
    with pytest.raises(ValueError, match="at least 10"):
        library.save(mine, {"size": 2})
    with pytest.raises(ValueError, match="at least 10"):
        library.save("rm:punch", {"size": 2})
    assert rm.load_template("mine")["size"] == 80 and rm.load_template("punch")["size"] == 92
    with pytest.raises(ValueError, match="nothing to set"):
        library.save("word", {"size": 80})
    with pytest.raises(NotFound):
        library.save("rm:gone", {"size": 80})
    # entries of the file that cannot be templates are left out, not crashed on
    tree = _local(cfgdir)
    tree["render"]["rm"]["presets"].update(
        {"punch": {"name": "x", "base": "jelly"}, "Bad Id": {"name": "x", "base": "punch"}, "lost": "?"}
    )
    tree["render"]["rm"]["presets"]["orphan"] = {"name": "Orphan", "base": "not-there"}
    config.save_local(tree)
    assert list(rm.presets()) == ["mine"] and rm.load_template("punch")["title"] == "Punch"
    assert [s["id"] for s in captions.library()["styles"] if s["own"]] == ["rm:mine"]


def test_saving_renaming_and_resetting(cfgdir):
    mine = library.create("rm:punch", "Mine", {"size": 80, "tilt": 3})["id"]
    after = library.save(mine, {"size": 70})  # the whole of its settings: the tilt is as it came again
    f = {x["path"]: x for g in after["groups"] for x in g["fields"]}
    assert f["size"]["value"] == 70 and f["tilt"]["value"] == 0 and after["name"] == "Mine"
    created = rm.presets()["mine"]["created"]
    assert created > 0 and _local(cfgdir)["render"]["rm"]["presets"]["mine"]["settings"] == {"size": 70}
    # a new name; the style stays, so what names it still finds it
    row = library.rename(mine, " Loud  one ")
    assert row["id"] == "rm:mine" and row["name"] == "Loud one" and rm.presets()["mine"]["created"] == created
    assert library.rename(mine, "LOUD ONE")["name"] == "LOUD ONE"  # its own name in other letters
    library.create("rm:jelly", "Second")
    with pytest.raises(ValueError, match="already a template called second"):
        library.rename(mine, "second")
    with pytest.raises(ValueError, match="Only a template of your own can be renamed"):
        library.rename("rm:punch", "Other")
    with pytest.raises(NotFound):
        library.rename("rm:gone", "Other")
    # a shipped template: this machine's edit of it, and back to how it came
    assert library.save("rm:punch", {"size": 100})["tuned"] is True and _by_id("rm:punch")["tuned"] is True
    assert rm.load_template("punch")["size"] == 100 and rm.load_template("mine")["size"] == 70
    assert library.reset("rm:punch")["tuned"] is False and rm.load_template("punch")["size"] == 92
    with pytest.raises(ValueError, match="no earlier version"):
        library.reset(mine)
    with pytest.raises(ValueError):
        library.reset("word")
    # the other settings of this machine pass through all of it
    settings.save({"detector.max_per_hour": 5, "render.captions": mine}, merge=True)
    assert library.save(mine, {})["groups"] and rm.presets()["mine"]["settings"] == {}
    settings.save({"detector.max_per_hour": 6}, merge=True)  # an own template with no settings is kept
    settings.save({"detector.max_per_hour": 6})  # the Settings page: every other field back to global
    assert list(rm.presets()) == ["mine", "second"] and rm.presets()["mine"]["name"] == "LOUD ONE"
    assert config.global_config()["detector"]["max_per_hour"] == 6


def test_the_style_of_new_clips_and_what_deleting_it_does(cfgdir):
    mine = library.create("rm:jelly", "Mine", {"size": 80})["id"]
    assert library.use(mine, "fixed") == {"default": "rm:mine", "colors": "fixed"}
    assert config.global_config()["render"]["captions"] == "rm:mine" and rm.color_mode() == "fixed"
    assert library.use("pop") == {"default": "pop", "colors": "fixed"}
    assert library.use(mine)["default"] == "rm:mine"
    with pytest.raises(ValueError, match="Colours"):
        library.use(mine, "rainbow")
    with pytest.raises(NotFound):
        library.use("rm:gone")
    with pytest.raises(ValueError, match="Only a template of your own can be deleted"):
        library.delete("rm:jelly")
    # deleted while it is the style of new clips: they go back to the template it was made from
    assert library.delete(mine) == {"deleted": "rm:mine", "default": "rm:jelly", "moved": True}
    assert rm.presets() == {} and not captions.is_style(mine)
    assert config.global_config()["render"]["captions"] == "rm:jelly"
    assert "presets" not in _local(cfgdir)["render"]["rm"]
    with pytest.raises(NotFound):
        library.delete(mine)
    other = library.create("rm:punch", "Other")["id"]
    assert library.delete(other) == {"deleted": "rm:other", "default": "rm:jelly", "moved": False}
    # a style that names a template that is gone is no style: a render falls back (test_remotion)
    with pytest.raises(rm.RemotionError, match="unknown caption template 'other'"):
        rm.load_template("other")


def test_a_preview_is_named_by_what_it_shows(tmp_path, cfgdir, made):
    root = tmp_path / "previews"
    shipped = captions.preview("rm:punch", None, root)["file"]
    mine = library.create("rm:punch", "Mine", {"size": 120})["id"]
    first = captions.preview(mine, None, root)["file"]
    assert first != shipped and first.name.startswith("punch-") and captions.CLIP_NAME.fullmatch(first.name)
    assert made[-1][1] == "mine" and made[-1][2]["size"] == 120  # rendered under its id, with its settings
    # the look it was saved from, asked for while it was being made, is the same clip: nothing new is made
    assert captions.preview("rm:punch", {"size": 120}, root, whole=True)["file"] == first and len(made) == 2
    # its saved settings change: another clip
    library.save(mine, {"size": 60})
    second = captions.preview(mine, None, root)["file"]
    assert second != first and made[-1][2]["size"] == 60 and len(made) == 3
    # a change to what it holds, and the whole laid over the base as it shipped
    assert captions.preview(mine, {"tilt": 2}, root)["file"] != second and made[-1][2]["size"] == 60
    settings.save_template("punch", {"size": 100})
    assert captions.preview("rm:punch", {"tilt": 2}, root)["file"] and made[-1][2]["size"] == 100
    assert captions.preview("rm:punch", {"tilt": 2}, root, whole=True)["file"] and made[-1][2]["size"] == 92
    library.delete(mine)
    with pytest.raises(ValueError, match="Pick a caption style"):
        captions.preview(mine, None, root)


def test_the_renderer_is_asked_for_the_base_of_an_own_template(cfgdir):
    library.create("rm:punch", "Mine", {"size": 120, "paging.maxWords": 2})
    from clipwatcher.models import Word

    words = [Word(0.2, 0.5, "no"), Word(0.5, 0.9, "way"), Word(1.0, 1.4, "really")]
    box = rm.caption_box(960, "seam")
    props, plan = rm.build_props(words, 3.0, "mine", box=box, fps=30, colors="fixed")
    assert props["template"] == "punch" and props["params"]["size"] == 120 and len(plan.pages) == 2
    # one video's own settings (a clip rendered again) are laid over the own template's
    with rm.overriding({"templates": {"mine": {"size": 60}}}):
        p = rm.load_template("mine")
        assert p["size"] == 60 and p["paging"]["maxWords"] == 2
    assert rm.load_template("mine")["size"] == 120


# ------------------------------------------------------------------ over the wire


@pytest.fixture
def served(tmp_path, monkeypatch, cfgdir, made):
    monkeypatch.setenv("CLIPWATCHER_STAGING", str(tmp_path / "staging"))
    monkeypatch.setenv("CLIPWATCHER_UI_WORK", str(tmp_path / "work"))
    server, app = make_server("127.0.0.1", 0, App(RunManager(tmp_path / "runs")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], app
    server.shutdown()
    server.server_close()


def _call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers or {})
    resp = conn.getresponse()
    body = resp.read()
    return resp.status, json.loads(body or b"{}") if "json" in resp.getheader("Content-Type", "") else body


def test_the_library_is_served_and_changed_through_the_page(served, cfgdir):
    port, app = served
    ok = {"X-Companion-Token": app.token, "Content-Type": "application/json"}

    def post(what, **body):
        return _call(port, "POST", f"/api/library/{what}", body, ok)

    status, lib = _call(port, "GET", "/api/library")
    assert status == 200 and len(lib["styles"]) == 29 and lib["name_max"] == 40 and lib["default"] == "word"
    assert not any(s["own"] or s["tuned"] for s in lib["styles"])
    assert _call(port, "POST", "/api/library/create", {"from": "rm:punch", "name": "Mine"})[0] == 403
    status, row = post("create", **{"from": "rm:punch"}, name="Neon night", settings={"size": 120})
    assert status == 200 and row["id"] == "rm:neon_night" and row["own"] is True
    status, err = post("create", **{"from": "rm:punch"}, name="Neon night")
    assert status == 400 and "already a template called Neon night" in err["error"]
    assert post("create", **{"from": "rm:punch"}, name="Bad", settings={"size": 2})[0] == 400
    assert post("create", **{"from": "rm:punch"}, name="Bad", settings=[1])[0] == 400
    # it is in the library, in the overview the Run walk and the clip drawer read, and has its fields
    styles = _call(port, "GET", "/api/overview")[1]["captions"]
    assert styles[-1]["id"] == "rm:neon_night" and styles[-1]["name"] == "Neon night"
    status, f = _call(port, "GET", "/api/library/fields?style=rm%3Aneon_night")
    assert status == 200 and f["own"] is True and f["base_name"] == "Punch"
    assert _call(port, "GET", "/api/library/fields?style=word")[0] == 400
    # previews: its own, and a look that is not saved yet
    status, shown = post("preview", style="rm:neon_night")
    assert status == 200 and shown["url"].startswith("/captions/clip/punch-")
    status, same = post("preview", style="rm:punch", settings={"size": 120}, whole=True)
    assert same["url"] == shown["url"]
    assert post("preview", style="rm:punch", settings={"size": 2}, whole=True)[0] == 400
    assert _call(port, "GET", shown["url"])[1][4:12] == b"ftypmp42"
    # save, rename, use, reset, delete
    assert post("save", style="rm:neon_night", settings={"size": 70})[0] == 200
    assert rm.load_template("neon_night")["size"] == 70
    assert post("rename", style="rm:neon_night", name="Night")[1]["name"] == "Night"
    assert post("use", style="rm:neon_night", colors="fixed")[1] == {
        "default": "rm:neon_night",
        "colors": "fixed",
    }
    assert _call(port, "GET", "/api/overview")[1]["caption_default"] == "rm:neon_night"
    assert post("save", style="rm:punch", settings={"size": 100})[1]["tuned"] is True
    assert post("reset", style="rm:punch")[1]["tuned"] is False
    assert post("delete", style="rm:punch")[0] == 400
    assert post("delete", style="rm:neon_night")[1] == {
        "deleted": "rm:neon_night",
        "default": "rm:punch",
        "moved": True,
    }
    assert post("delete", style="rm:neon_night")[0] == 404
    assert post("nothing")[0] == 404 and _call(port, "GET", "/api/library/nothing")[0] == 404
    assert len(_call(port, "GET", "/api/library")[1]["styles"]) == 29
