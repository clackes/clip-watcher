"""The companion app's caption library: every style, what can be set on an animated template and within
what limits, previews, and a clip rendered again with a template of its own. No Node is needed here: the
renderer is stood in for; one test at the end makes a real preview where the Remotion setup exists."""

import http.client
import json
import subprocess
import threading

import pytest

from clipwatcher import config
from clipwatcher.render import remotion as rm
from clipwatcher.render import template
from clipwatcher.ui import captions, jobs, settings
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
        calls.append((style, params))
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(40))
        return ["a warning"] if params and params["size"] > 200 else []

    monkeypatch.setattr(captions, "_make", make)
    monkeypatch.setattr(captions, "_backdrop", lambda root: root / "backdrop.png")
    monkeypatch.setattr(rm, "missing", lambda: None)
    return calls


# ------------------------------------------------------------------ the library and its limits


def test_the_library_is_the_built_in_styles_and_every_template():
    lib = captions.library()
    ids = [s["id"] for s in lib["styles"]]
    assert ids[:4] == ["word", "pop", "classic", "bounce"] and tuple(ids) == captions.styles()
    animated = [s for s in lib["styles"] if s["kind"] == "animated"]
    assert len(animated) == len(rm.templates()) == 25 and "rm:punch" in ids
    assert all(s["name"] and s["about"] and s["font"] and 1 <= s["words"] <= 8 for s in animated)
    assert lib["default"] == "word" and lib["colors"] in captions.COLOUR_MODES and "npm ci" in lib["setup"]
    assert captions.is_style("rm:jelly") and not captions.is_style("rm:nope") and not captions.is_style(None)


def test_every_parameter_of_every_template_can_be_set_and_its_limits_are_known():
    names = rm.templates()
    total = 0
    for name in names:
        own, specs = captions._own(name), captions._specs(name)
        leaves = [
            f"{k}.{kk}" if isinstance(v, dict) else k
            for k, v in own.items()
            if k not in captions.HIDDEN
            for kk in (v if isinstance(v, dict) else [None])
        ]
        assert sorted(leaves) == sorted(specs), (name, set(leaves) ^ set(specs))
        total += len(leaves)
        for path, spec in specs.items():
            value = captions._get(own, path)
            if spec["kind"] == "number":  # what the template ships with is inside its own limits
                assert spec.get("min", value) <= value <= spec.get("max", value), (name, path, value, spec)
            elif spec["kind"] == "select":
                assert value in spec["options"], (name, path)
        # an effect without both ends would be a slider the page has to guess: there are a handful at most
        open_ended = [
            p for p, s in specs.items() if p.startswith("fx.") and s["kind"] == "number" and "max" not in s
        ]
        assert len(open_ended) <= 2, (name, open_ended)
    assert total > 1500  # 25 templates, some sixty settings each


def test_the_animation_library_is_read_with_its_limits_and_defaults():
    lib = captions.animations()
    assert {n: a["slot"] for n, a in lib.items()} == rm.animations() and len(lib) == 25
    assert {a["slot"] for a in lib.values()} == set(rm.SLOTS)
    for name, a in lib.items():
        assert a["about"] and a["params"], name
        for key, p in a["params"].items():
            assert p["min"] <= p["default"] <= p["max"], (name, key, p)
        assert bool(a["units"]) == (a["slot"] in ("enter", "exit")), name
    assert lib["slide"]["units"] == ["page"] and "letter" in lib["pop"]["units"]


def test_settings_from_the_page_are_checked_against_the_templates_own_limits():
    ok = captions.check(
        "rm:punch",
        {
            "size": 120,
            "paging.maxWords": 2,
            "case": "lower",
            "colors.active": "#00e5ff",
            "fx.enter": {"name": "drop", "by": "letter", "params": {"height": 1.2}},
            "fx.idle": None,
        },
    )
    assert ok == {
        "size": 120.0,
        "paging": {"maxWords": 2},
        "case": "lower",
        "colors": {"active": "#00E5FF"},
        "fx": {"enter": {"name": "drop", "params": {"height": 1.2}, "by": "letter"}, "idle": None},
    }
    assert isinstance(ok["paging"]["maxWords"], int)
    refused = {
        "a size under the smallest": ({"size": 5}, "at least 10"),
        "a setting it does not have": ({"nope": 1}, "no setting"),
        "the font file": ({"font": {"file": "x.ttf"}}, "no setting"),
        "half a word": ({"paging.maxWords": 2.5}, "whole number"),
        "a word for a number": ({"size": "big"}, "a number"),
        "true for a number": ({"size": True}, "a number"),
        "letters outside the choices": ({"case": "shouty"}, "one of"),
        "not a colour": ({"colors.text": "red"}, "a colour like"),
        "dark words on the dark outline": ({"colors.text": "#111111"}, "at least 4.5:1"),
        "an animation of another slot": ({"fx.enter": {"name": "shrink"}}, "one of pop"),
        "a slide letter by letter": ({"fx.enter": {"name": "slide", "by": "letter"}}, "the whole phrase"),
        "a setting the animation lacks": ({"fx.enter": {"name": "pop", "params": {"spin": 1}}}, "no setting"),
        "an animation value out of range": ({"fx.enter": {"name": "pop", "params": {"from": 3}}}, "between"),
        "no entrance at all": ({"fx.enter": None}, "pick an animation"),
    }
    for what, (values, says) in refused.items():
        with pytest.raises(ValueError, match=says):
            captions.check("rm:punch", values)
        assert what
    with pytest.raises(ValueError, match="animated templates"):
        captions.check("word", {"size": 80})
    assert captions.check("rm:punch", None) == {} and captions.check("word", {}) == {}


def test_fields_show_the_value_in_force_beside_the_templates_own(cfgdir):
    f = captions.fields("rm:punch")
    by_path = {x["path"]: x for g in f["groups"] for x in g["fields"]}
    assert [g["id"] for g in f["groups"]] == [
        "text",
        "paging",
        "timing",
        "colors",
        "motion",
        "fx",
        "label",
        "room",
    ]
    assert f["name"] == "Punch" and f["tuned"] is False
    size = by_path["size"]
    assert (
        size["value"] == size["own"] == 92
        and size["min"] == 10
        and size["soft_max"] >= 200
        and size["step"] == 1
    )
    assert by_path["paging.maxWords"]["whole"] and by_path["paging.maxWords"]["max"] == 8
    assert {o["id"] for o in by_path["case"]["options"]} == {"upper", "lower", "title", "keep"}
    enter = by_path["fx.enter"]
    assert enter["kind"] == "motion" and enter["value"]["name"] == "pop" and not enter["optional"]
    assert {c["id"] for c in enter["choices"]} == {n for n, s in rm.animations().items() if s == "enter"}
    assert by_path["fx.idle"]["optional"] and by_path["fx.idle"]["value"] is None
    assert by_path["colors.cycle"]["kind"] == "colors" and by_path["colors.text"]["kind"] == "color"
    # this machine's own version of the template: kept in local.yaml, shown as the value in force
    settings.save_template("punch", captions.check("rm:punch", {"size": 110, "fx.halo": 0.14}))
    f = captions.fields("rm:punch")
    by_path = {x["path"]: x for g in f["groups"] for x in g["fields"]}
    assert f["tuned"] and by_path["size"]["value"] == 110 and by_path["size"]["own"] == 92
    assert by_path["fx.halo"]["value"] == 0.14 and rm.load_template("punch")["size"] == 110
    assert next(s for s in captions.library()["styles"] if s["id"] == "rm:punch")["tuned"] is True
    # the page's other settings are saved without losing it, and none puts the template back
    settings.save({"render.captions": "rm:punch", "render.rm.max_fps": 30, "render.rm.colors": "fixed"})
    local = config.local_overrides()["render"]
    assert local["rm"]["templates"]["punch"] == {"size": 110.0, "fx": {"halo": 0.14}}
    assert local["captions"] == "rm:punch" and local["rm"]["max_fps"] == 30 and rm.color_mode() == "fixed"
    with pytest.raises(ValueError, match="render.captions"):
        settings.save({"render.captions": "rm:nope"})
    settings.save_template("punch", {})
    assert (
        "templates" not in config.local_overrides()["render"]["rm"]
        and rm.load_template("punch")["size"] == 92
    )
    with pytest.raises(ValueError, match="animated templates"):
        captions.fields("word")


# ------------------------------------------------------------------ previews


def test_a_preview_is_made_once_and_named_by_what_it_shows(tmp_path, made):
    first = captions.preview("rm:punch", None, tmp_path)
    again = captions.preview("rm:punch", None, tmp_path)
    assert first["file"] == again["file"] and first["file"].is_file() and len(made) == 1
    assert first["warnings"] == [] and captions.CLIP_NAME.fullmatch(first["file"].name)
    tuned = captions.preview("rm:punch", {"size": 240}, tmp_path)
    assert (
        tuned["file"] != first["file"] and made[-1][1]["size"] == 240 and tuned["warnings"] == ["a warning"]
    )
    assert (
        captions.preview("rm:punch", {"size": 240}, tmp_path)["warnings"] == ["a warning"] and len(made) == 2
    )
    built_in = captions.preview("word", None, tmp_path)
    assert built_in["file"].name.startswith("word-") and made[-1] == ("word", None)
    # only names this module gives out are looked up
    assert captions.clip_path(first["file"].name, tmp_path) == first["file"]
    for name in ("../config/global.yaml", "punch.mp4", first["file"].name.replace(".mp4", ".json"), ""):
        assert captions.clip_path(name, tmp_path) is None
    with pytest.raises(ValueError, match="Pick a caption style"):
        captions.preview("rm:nope", None, tmp_path)
    with pytest.raises(ValueError, match="at least 10"):
        captions.preview("rm:punch", {"size": 1}, tmp_path)
    assert captions.warm(tmp_path) == len(captions.styles())  # the rest, made in the background by the app


def test_without_the_setup_the_animated_previews_say_what_to_run(tmp_path, made, monkeypatch):
    monkeypatch.setattr(rm, "missing", lambda: "node not found")
    with pytest.raises(ValueError, match="npm ci"):
        captions.preview("rm:punch", None, tmp_path)
    assert captions.preview("pop", None, tmp_path)["file"].is_file()  # the built-in ones need ffmpeg only
    lib = captions.library()
    assert lib["ready"] is False and lib["missing"] == "node not found"
    assert captions.warm(tmp_path) == 4


def test_settings_for_one_render_are_laid_over_the_config_and_taken_off_again(cfgdir):
    assert rm.color_mode() == "auto" and rm.load_template("punch")["size"] == 92
    with rm.overriding(
        {"colors": "fixed", "templates": {"punch": {"size": 70, "fx": {"enter": {"name": "drop"}}}}}
    ):
        p = rm.load_template("punch")
        assert rm.color_mode() == "fixed" and p["size"] == 70
        assert p["fx"]["enter"] == {"params": {}, "name": "drop"}  # another animation: the block whole
        with rm.overriding({"templates": {"punch": {"size": 60}}}):
            assert rm.load_template("punch")["size"] == 60 and rm.color_mode() == "fixed"
        assert rm.load_template("punch")["size"] == 70
    assert rm.color_mode() == "auto" and rm.load_template("punch")["size"] == 92


def test_a_short_says_which_caption_style_it_got(monkeypatch, tmp_path):
    seen = []
    encode = lambda style, overlay: seen.append((style, overlay))  # noqa: E731
    box = rm.caption_box(960, "seam")
    assert template._encode_with_captions(encode, "pop", tmp_path, [], 5.0, 30.0, box, None, None, None) == {
        "captions": "pop"
    }
    monkeypatch.setattr(rm, "color_mode", lambda: "fixed")
    monkeypatch.setattr(
        rm, "render_overlay", lambda *a, **k: (_ for _ in ()).throw(rm.RemotionError("no node"))
    )
    got = template._encode_with_captions(encode, "rm:punch", tmp_path, [], 5.0, 30.0, box, None, None, None)
    assert got == {"captions": "word", "captions_error": "no node"} and seen[-1] == ("word", None)
    overlay = rm.Overlay(tmp_path, "%06d.png", 30.0, 150, 1080, 256, 0, 770)
    monkeypatch.setattr(rm, "render_overlay", lambda *a, **k: overlay)
    got = template._encode_with_captions(encode, "rm:punch", tmp_path, [], 5.0, 30.0, box, None, None, None)
    assert got == {"captions": "rm:punch"} and seen[-1] == ("rm:punch", overlay)


# ------------------------------------------------------------------ a clip again, with a template of its own


def _staged(root, cid="zack-live-1", **meta):
    d = root / cid
    d.mkdir(parents=True)
    base = {"streamer": "zack", "title": "A title", "hook": "A hook", "state": "READY", "captions": "word"}
    (d / "meta.json").write_text(json.dumps({**base, **meta}))
    (d / "short_9x16.mp4").write_bytes(b"old short")
    (d / "clean_16x9.mp4").write_bytes(b"clean")
    return d


@pytest.fixture
def rendering(monkeypatch, tmp_path, cfgdir):
    """render_short stood in for: it notes what the renderer would have been told, read as it reads it."""
    calls = []
    outcome = {}

    def render_short(src, start_s, dur_s, out, *, captions, report=None, **kw):
        name = rm.template_name(captions) if rm.is_rm(captions) else None
        calls.append(
            {
                "captions": captions,
                "colors": rm.color_mode(),
                "params": rm.load_template(name) if name else None,
            }
        )
        out.write_bytes(b"new short")
        if report is not None:
            report.update({"captions": captions, **outcome})
        return out

    monkeypatch.setattr("clipwatcher.render.template.render_short", render_short)
    monkeypatch.setattr(jobs, "_duration", lambda media: 38.0)
    monkeypatch.setattr(jobs.recipe, "read", lambda folder: {"words": ["w"], "hook": "A hook"})
    monkeypatch.setattr(rm, "missing", lambda: None)
    return calls, outcome


def test_a_clip_is_rendered_again_with_a_template_colours_and_settings_of_its_own(tmp_path, rendering):
    calls, _ = rendering
    root = tmp_path / "staging"
    d = _staged(root)
    mine = {"size": 80, "fx.active": {"name": "hop", "params": {"height": 0.5}}}
    jobs.rerender("zack-live-1", captions="rm:punch", root=root, colors="fixed", settings=mine)
    (call,) = calls
    assert call["captions"] == "rm:punch" and call["colors"] == "fixed" and call["params"]["size"] == 80
    assert call["params"]["fx"]["active"] == {"name": "hop", "params": {"height": 0.5}}
    assert rm.color_mode() == "auto" and rm.load_template("punch")["size"] == 92  # for that render only
    meta = json.loads((d / "meta.json").read_text())
    assert (meta["captions"], meta["caption_colors"], meta["caption_settings"]) == ("rm:punch", "fixed", mine)
    assert (d / "short_9x16.mp4").read_bytes() == b"new short"
    # the same template again with nothing said keeps the config's colour mode; a built-in style drops both
    jobs.rerender("zack-live-1", root=root)
    assert (
        calls[-1]["captions"] == "rm:punch"
        and calls[-1]["colors"] == "auto"
        and calls[-1]["params"]["size"] == 92
    )
    jobs.rerender("zack-live-1", captions="pop", root=root)
    meta = json.loads((d / "meta.json").read_text())
    assert meta["captions"] == "pop" and "caption_colors" not in meta and "caption_settings" not in meta


def test_a_template_that_cannot_be_rendered_leaves_the_clip_as_it_was(tmp_path, rendering, monkeypatch):
    calls, outcome = rendering
    root = tmp_path / "staging"
    d = _staged(root)
    outcome.update(captions="word", captions_error="render failed (1): boom")  # the renderer fell back
    with pytest.raises(ValueError, match="could not be rendered: render failed"):
        jobs.rerender("zack-live-1", captions="rm:punch", root=root)
    assert (d / "short_9x16.mp4").read_bytes() == b"old short" and not (d / "short_9x16.new.mp4").exists()
    assert json.loads((d / "meta.json").read_text())["captions"] == "word"
    for kw, says in (
        ({"captions": "rm:nope"}, "Caption style"),
        ({"captions": "pop", "colors": "fixed"}, "for the animated templates"),
        ({"captions": "pop", "settings": {"size": 80}}, "for the animated templates"),
        ({"captions": "rm:punch", "colors": "pink"}, "Caption colours"),
        ({"captions": "rm:punch", "settings": {"size": 2}}, "at least 10"),
    ):
        with pytest.raises(ValueError, match=says):
            jobs.rerender("zack-live-1", root=root, **kw)
    monkeypatch.setattr(rm, "missing", lambda: "node not found")
    with pytest.raises(ValueError, match="one-time setup"):
        jobs.rerender("zack-live-1", captions="rm:punch", root=root)
    assert len(calls) == 1 and (d / "short_9x16.mp4").read_bytes() == b"old short"


def test_the_render_command_carries_a_templates_settings_checked():
    mine = {"size": 80, "paging.maxWords": 2}
    cmd = command(
        "render", {"clip": "zack-live-1", "captions": "rm:punch", "colors": "fixed", "settings": mine}
    )
    assert cmd[-7:] == [
        "zack-live-1",
        "--captions",
        "rm:punch",
        "--colors",
        "fixed",
        "--settings",
        json.dumps(mine, sort_keys=True),
    ]
    assert "--settings" not in command(
        "render", {"clip": "zack-live-1", "captions": "rm:punch", "settings": {}}
    )
    for params, says in (
        ({"captions": "rm:nope"}, "captions: one of"),
        ({"captions": "rm:punch", "colors": "pink"}, "colors: one of"),
        ({"captions": "rm:punch", "settings": {"size": 2}}, "at least 10"),
        ({"captions": "pop", "settings": {"size": 80}}, "animated templates"),
        ({"settings": {"size": 80}}, "animated templates"),
    ):
        with pytest.raises(ValueError, match=says):
            command("render", {"clip": "zack-live-1", **params})


def test_the_job_reads_its_settings_from_the_command_line(tmp_path, rendering, monkeypatch, capsys):
    calls, _ = rendering
    monkeypatch.setenv("CLIPWATCHER_STAGING", str(tmp_path / "staging"))
    _staged(tmp_path / "staging")
    args = [
        "render",
        "zack-live-1",
        "--captions",
        "rm:jelly",
        "--colors",
        "fixed",
        "--settings",
        '{"size": 70}',
    ]
    assert jobs.main(args) == 0 and calls[-1]["params"]["size"] == 70 and calls[-1]["colors"] == "fixed"
    assert jobs.main(["render", "zack-live-1", "--captions", "rm:nope"]) == 1
    assert "not rendered: Caption style" in capsys.readouterr().out


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
    return resp.status, dict(resp.getheaders()), resp.read()


def test_the_library_its_settings_and_previews_are_served(served, cfgdir):
    port, app = served
    ok = {"X-Companion-Token": app.token, "Content-Type": "application/json"}
    lib = json.loads(_call(port, "GET", "/api/captions")[2])
    assert len(lib["styles"]) == 29 and all(s["preview"] is None for s in lib["styles"])
    assert json.loads(_call(port, "GET", "/api/overview")[2])["caption_library"]["ready"] is True
    status, _, body = _call(port, "GET", "/api/captions/fields?style=rm%3Apunch")
    assert status == 200 and json.loads(body)["name"] == "Punch"
    assert _call(port, "GET", "/api/captions/fields?style=word")[0] == 400
    # a preview: asked for with the page's token, then fetched by its name, whole or in part
    assert _call(port, "POST", "/api/captions/preview", {"style": "rm:punch"})[0] == 403
    status, _, body = _call(port, "POST", "/api/captions/preview", {"style": "rm:punch"}, ok)
    url = json.loads(body)["url"]
    assert status == 200 and url.startswith("/captions/clip/punch-") and json.loads(body)["warnings"] == []
    status, headers, part = _call(port, "GET", url, headers={"Range": "bytes=4-11"})
    assert status == 206 and part == b"ftypmp42" and headers["Content-Type"] == "video/mp4"
    assert "immutable" in headers["Cache-Control"]
    assert _call(port, "GET", "/captions/clip/..%2F..%2Fconfig%2Fglobal.yaml")[0] == 404
    status, _, body = _call(
        port, "POST", "/api/captions/preview", {"style": "rm:punch", "settings": {"size": 2}}, ok
    )
    assert status == 400 and "at least 10" in json.loads(body)["error"]
    styles = json.loads(_call(port, "GET", "/api/captions")[2])["styles"]
    assert next(s for s in styles if s["id"] == "rm:punch")["preview"] == url  # there now: played at once
    # this machine's own version of a template
    status, _, body = _call(
        port, "POST", "/api/captions/defaults", {"style": "rm:punch", "settings": {"size": 100}}, ok
    )
    assert status == 200 and json.loads(body)["tuned"] is True and rm.load_template("punch")["size"] == 100
    assert (cfgdir / "local.yaml").is_file() and "size: 100" in (cfgdir / "local.yaml").read_text()
    assert _call(port, "POST", "/api/captions/defaults", {"style": "word", "settings": {}}, ok)[0] == 400
    assert (
        _call(port, "POST", "/api/captions/defaults", {"style": "rm:punch", "settings": {"x": 1}}, ok)[0]
        == 400
    )
    status, _, body = _call(port, "POST", "/api/captions/defaults", {"style": "rm:punch", "settings": {}}, ok)
    assert json.loads(body)["tuned"] is False and rm.load_template("punch")["size"] == 92


# ------------------------------------------------------------------ the real thing


@pytest.mark.remotion
@pytest.mark.ffmpeg
@pytest.mark.skipif(not rm.available(), reason="needs the Remotion setup (remotion/README.md)")
def test_a_real_preview_of_an_animated_template_and_of_a_built_in_style(tmp_path):
    for style, values in (("rm:punch", {"fx.enter": {"name": "drop", "by": "letter"}}), ("pop", None)):
        got = captions.preview(style, values, tmp_path)
        probe = subprocess.run(
            [config.ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(got["file"])],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        info = json.loads(probe.stdout)
        stream = info["streams"][0]
        assert (stream["width"], stream["height"]) == (captions.PREVIEW_W, captions.PREVIEW_H)
        assert (
            stream["r_frame_rate"] == "60/1"
            and abs(float(info["format"]["duration"]) - captions.SAMPLE_S) < 0.1
        )
        assert got["warnings"] == [] and got["file"].stat().st_size > 20_000
