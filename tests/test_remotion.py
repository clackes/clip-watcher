"""The Python side of the Remotion captions: the plan, the props, where captions go, and the fallback.

None of this needs Node: the planner and the props are pure Python, and the render path is exercised with
Remotion reported missing (a clip must never fail because of captions)."""

from __future__ import annotations

import json
import logging

import pytest

from clipwatcher import config
from clipwatcher.models import Word
from clipwatcher.render import captions, rm_plan, template
from clipwatcher.render import framecheck as fc
from clipwatcher.render import remotion as rm

pytestmark = pytest.mark.skipif(
    not (rm.TEMPLATES_DIR / "punch.json").is_file(), reason="no remotion/templates"
)


def punch() -> dict:
    return rm.load_template("punch")


def raw_punch() -> dict:
    return json.loads((rm.TEMPLATES_DIR / "punch.json").read_text())


def plan_of(words, dur, params=None, box="seam", **kw):
    P = params or punch()
    b = rm.caption_box({"seam": 960, "frame": 1450, "reframe": 1250, "stack": 1495}[box], box)
    return rm_plan.plan(
        words, dur, P, box_w=b.w - 2 * P["margin"]["x"], box_h=b.h - 2 * P["margin"]["y"], **kw
    )


# ------------------------------------------------------------------ text


def test_clean_text_normalises_and_keeps_only_what_the_font_can_draw():
    P = punch()
    assert rm_plan.clean_text(" don’t, ", P) == "DON'T"  # curly quote, case, trailing comma
    assert rm_plan.clean_text("what?!", P) == "WHAT?!"  # ? and ! stay
    assert rm_plan.clean_text("100%", P) == "100%"
    assert rm_plan.clean_text("wait…", P) == "WAIT"  # ellipsis -> ... -> stripped as trailing stops
    assert rm_plan.clean_text("...", P) == "" and rm_plan.clean_text("[music]", P) != ""
    assert rm_plan.clean_text("你好ok", P) == "OK"  # no glyph, no fallback font: dropped
    assert rm_plan.clean_text("lol\U0001f602", P) == "LOL"  # no emoji font given: emoji are taken out
    assert rm_plan.clean_text("lol\U0001f602", P, frozenset({0x1F602})) == "LOL\U0001f602"
    assert (
        rm_plan.clean_text("\U0001f602", P, frozenset({0x1F602})) == "\U0001f602"
    )  # an emoji alone is a word
    lower = {**P, "case": "lower"}
    assert rm_plan.clean_text("NO Way", lower) == "no way".replace(" ", "")  # one token: spaces never survive


def test_emoji_are_always_their_own_unit():
    e = frozenset({0x1F602, 0x1F480})
    assert rm_plan.split_emoji("GOOO\U0001f602\U0001f480", e) == ["GOOO", "\U0001f602", "\U0001f480"]
    assert rm_plan.split_emoji("\U0001f602OK", e) == ["\U0001f602", "OK"]
    assert rm_plan.split_emoji("PLAIN", e) == ["PLAIN"]
    pl = plan_of([Word(0.2, 0.8, "gooo\U0001f602")], 2.0, emoji=e)
    assert [w.text for p in pl.pages for w in p.words] == ["GOOO", "\U0001f602"]


def test_emoji_come_only_from_the_apple_font(monkeypatch, tmp_path):
    monkeypatch.setattr(rm, "EMOJI_FILE", tmp_path / "missing.ttc")
    assert rm.emoji_family() == "" and rm.emoji_set() == frozenset()
    props, pl = rm.build_props(
        [Word(0.1, 0.6, "lol\U0001f602")], 1.0, "punch", box=rm.caption_box(960, "seam"), fps=30
    )
    assert props["emojiFamily"] == "" and [w["text"] for w in props["pages"][0]["words"]] == ["LOL"]


def test_font_reader_knows_what_a_font_covers():
    have = rm_plan.font_chars(str(rm_plan.font_path(punch()["font"])))
    assert all(ord(c) in have for c in "AZaz09!?'%&") and 0x1F602 not in have and 0x4F60 not in have


def test_every_template_font_is_bundled_static_and_licensed():
    fonts_dir = config.ROOT / "assets" / "fonts"
    for name in rm.templates():
        P = rm.load_template(name)
        for spec in [P["font"], *([P["emphFont"]] if P["emphFont"] else [])]:
            f = fonts_dir / spec["file"]
            assert f.is_file(), f"{name}: {spec['file']} missing"
            have = rm_plan.font_chars(str(f))
            assert all(cp in have for cp in range(0x21, 0x7F) if chr(cp) not in "^`|~\\{}<>_"), (
                f"{name}: ASCII gaps"
            )
            family = spec["family"].replace(" ", "")
            licences = [p.name for p in f.parent.iterdir() if p.suffix == ".txt" and family in p.name]
            assert licences, f"{name}: no licence file for {spec['family']}"
            assert b"fvar" not in f.read_bytes()[:4096], f"{name}: {spec['file']} is a variable font"


# ------------------------------------------------------------------ the plan


def _check_plan(pl: rm_plan.Plan, dur: float, overlap: float = 0.0):
    spans = []
    for p in pl.pages:
        assert 0 <= p.start < p.end <= dur + 1e-6 and 0 <= p.exit <= p.end - p.start
        spans.append((p.start, p.end))
        prev_end = p.start
        for w in p.words:
            assert p.start - 1e-6 <= w.start < w.active_end <= p.end + 1e-6, (w, p.start, p.end)
            assert w.start >= prev_end - 1e-6  # one word highlighted at a time
            prev_end = w.active_end
    for (_, e0), (s1, _) in zip(spans, spans[1:], strict=False):
        assert s1 >= e0 - overlap - 1e-6  # pages follow each other
    for lb in pl.labels:
        assert all(
            not (p.start < lb.end and lb.start < p.end) for p in pl.pages
        )  # a label has the spot alone


@pytest.mark.parametrize("name", list(fc.FIXTURES))
def test_plan_of_every_fixture_is_consistent(name):
    fx = fc.FIXTURES[name]
    for tpl in rm.templates():
        P = rm.load_template(tpl)
        pl = plan_of(fx.word_list(), fx.dur, P, fx.box, beats=list(fx.beats), actions=list(fx.actions))
        _check_plan(pl, fx.dur, float(P["paging"]["overlapS"]))
        shown = sorted(w.src for p in pl.pages for w in p.words)
        assert sorted({*shown, *pl.dropped}) == list(
            range(len(fx.words))
        )  # every word is shown or accounted for


def test_words_start_on_their_timestamps():
    P = punch()
    lead = P["paging"]["leadS"]
    words = fc.FIXTURES["fast"].word_list()  # seven words a second, no gaps
    pl = plan_of(words, 3.0, P, "frame")
    got = {w.src: w for p in pl.pages for w in p.words}
    assert len(got) == len(words)
    for i, w in enumerate(words):
        assert got[i].start == pytest.approx(max(0.0, w.start - lead), abs=1e-3) and not got[i].moved
        assert got[i].active_end - got[i].start >= rm_plan.MIN_ACTIVE_S - 1e-6


def test_words_with_the_same_timestamp_are_spaced_three_frames_apart():
    pl = plan_of([Word(0.5, 0.9, "is"), Word(0.5, 0.8, "that"), Word(0.5, 1.0, "it")], 2.0)
    ws = [w for p in pl.pages for w in p.words]
    assert [w.text for w in ws] == ["IS", "THAT", "IT"]
    assert (
        ws[1].start - ws[0].start == pytest.approx(rm_plan.MIN_ACTIVE_S, abs=1e-3)
        and ws[1].moved
        and ws[2].moved
    )
    assert rm_plan.MIN_ACTIVE_S * 30 >= 3 - 1e-9  # three frames at 30 fps: a highlight can be animated


def test_nothing_is_planned_for_less_than_three_frames():
    """A page of one word that the next page replaces, a word the clip ends on: each is up for at least
    MIN_ACTIVE_S or is not shown (one frame of a word is a flash, whatever the template does with it)."""
    for tpl in ("slam", "reel", "punch", "bounce"):  # one word a page, three, four
        P = rm.load_template(tpl)
        words = [Word(0.2, 0.5, "what"), Word(0.5, 1.3, "is"), Word(0.5, 0.9, "that"), Word(1.96, 2.0, "bro")]
        pl = plan_of(words, 2.0, P)
        assert [w.text.upper() for p in pl.pages for w in p.words] == [
            "WHAT",
            "IS",
            "THAT",
        ] and pl.dropped == [3]
        for a, b in zip(pl.pages, pl.pages[1:], strict=False):
            assert b.start - a.start >= rm_plan.MIN_ACTIVE_S - 1e-6
        for p in pl.pages:
            assert p.end - p.start >= rm_plan.MIN_ACTIVE_S - 1e-6
            assert all(w.active_end - w.start >= rm_plan.MIN_ACTIVE_S - 1e-6 for w in p.words)


def test_pages_are_phrases():
    P = punch()
    words = [Word(*w) for w in fc._seq(0.2, 0.3, "No way! did you see that, chat? he went all in")]
    pages = [[w.text for w in p.words] for p in plan_of(words, 5.0, P).pages]
    # a sentence ends its page; three words at most; "that," opens a page, so its comma does not close it
    assert pages == [["NO", "WAY!"], ["DID", "YOU", "SEE"], ["THAT", "CHAT?"], ["HE", "WENT", "ALL"], ["IN"]]
    comma = [Word(*w) for w in fc._seq(0.2, 0.3, "see that, chat is")]
    assert [[w.text for w in p.words] for p in plan_of(comma, 3.0, P).pages] == [
        ["SEE", "THAT"],
        ["CHAT", "IS"],
    ]
    gap = [Word(0.1, 0.4, "one"), Word(0.4, 0.7, "two"), Word(2.0, 2.3, "three")]
    pl = plan_of(gap, 4.0, P)
    assert [[w.text for w in p.words] for p in pl.pages] == [["ONE", "TWO"], ["THREE"]]  # a pause splits
    assert pl.pages[0].exit > 0 and pl.pages[0].end < 2.0  # and the first page leaves with its exit animation


def test_continuous_speech_cuts_and_a_pause_gets_an_exit():
    words = [Word(*w) for w in fc._seq(0.1, 0.25, "a b c d e f g h")]
    pl = plan_of(words, 3.0)
    assert all(p.exit == 0 for p in pl.pages[:-1])  # the next page replaces this one: no exit in between
    assert pl.pages[-1].exit > 0  # silence after the last word: it animates out
    for a, b in zip(pl.pages, pl.pages[1:], strict=False):
        assert a.end == pytest.approx(b.start)


def test_long_words_get_a_page_of_their_own_and_always_fit():
    P = punch()
    box = rm.caption_box(1495, "stack")
    bw, bh = box.w - 2 * P["margin"]["x"], box.h - 2 * P["margin"]["y"]
    long = "PNEUMONOULTRAMICROSCOPICSILICO"
    size = rm_plan.fit_size([(long, False)], P, bw, bh)
    assert size < P["size"] and rm_plan.text_width(long, P["font"], size, P["letterSpacing"]) <= bw + 0.5
    pl = plan_of([Word(0.1, 0.4, "the"), Word(0.4, 1.0, long), Word(1.0, 1.3, "word")], 2.0, P, "stack")
    assert [long] in [[w.text for w in p.words] for p in pl.pages]


def test_labels_take_the_spot_and_beats_win_over_actions():
    words = [Word(0.2, 0.5, "before"), Word(1.2, 1.5, "under"), Word(2.6, 2.9, "after")]
    pl = plan_of(
        words,
        4.0,
        beats=[(1.0, "fail", "*FAIL*", "bonk")],
        actions=[(1.4, 2.2, "*STARES*"), (3.0, 3.6, "*WAITS*")],
    )
    assert [(lb.kind, lb.text) for lb in pl.labels] == [("fail", "*FAIL*"), ("action", "*WAITS*")]
    assert [w.text for p in pl.pages for w in p.words] == ["BEFORE", "AFTER"] and pl.dropped == [1]
    assert pl.labels[0].end == pytest.approx(1.0 + rm_plan.LABEL_S)


def test_empty_and_single_word_plans():
    assert plan_of([], 1.0).empty
    assert plan_of([Word(0.1, 0.2, "...")], 1.0).empty  # punctuation alone is nothing to show
    one = plan_of([Word(0.0, 0.6, "Bruh")], 1.4)
    assert len(one.pages) == 1 and one.pages[0].start == 0 and one.pages[0].exit > 0


# ------------------------------------------------------------------ where captions go, props


@pytest.mark.parametrize("kind,cap_y", [("seam", 960), ("frame", 1450), ("reframe", 1250), ("stack", 1495)])
def test_caption_box_stays_out_of_the_dead_zone_and_off_the_edges(kind, cap_y):
    b = rm.caption_box(cap_y, kind)
    assert b.x0 >= 60 and b.x1 <= 1080 - 60 and b.y1 < rm.DEAD_ZONE_Y and b.y0 >= 0
    assert b.y0 % 2 == 0 and b.h % 2 == 0 and b.h >= 100  # even rows: the band is blended in 4:2:0 video
    assert b.y0 <= b.anchor_y <= b.y1
    if kind == "seam":
        assert b.y1 <= template.SEAM_Y + 75 - 8  # clear of the credit under the seam
    if kind == "frame":
        assert b.y0 >= template.FRAME_Y + template.FRAME_H + 12 + 44  # under the credit below the frame


def test_props_are_complete_and_fixed_colours_are_the_templates():
    fx = fc.FIXTURES["mix"]
    box = rm.caption_box(960, "seam")
    props, pl = rm.build_props(
        fx.word_list(), fx.dur, "punch", box=box, fps=59.94, beats=list(fx.beats), colors="fixed"
    )
    assert set(props) == {
        "template", "params", "fps", "width", "height", "durationInFrames", "region", "pages", "labels",
        "emojiFamily", "debug",
    }  # fmt: skip
    assert (
        props["width"] == 1080
        and props["height"] == box.h
        and props["durationInFrames"] == round(fx.dur * 59.94)
    )
    assert props["region"] == {"x0": 60, "y0": 0, "x1": 1020, "y1": box.h, "anchorY": box.anchor_y - box.y0}
    P = punch()
    assert all(p["colors"] == P["colors"] and p["from"] is None for p in props["pages"])
    assert len(props["pages"]) == len(pl.pages) and props["labels"][0]["colors"]["text"] != ""
    json.dumps(props)  # serialisable as it is


def test_auto_colours_follow_the_picture_and_ease_between_phrases():
    box = rm.caption_box(960, "seam")
    yellow, grey = bytes((232, 196, 0)) * (rm.BG_W * rm.BG_H), bytes((40, 40, 48)) * (rm.BG_W * rm.BG_H)
    bg = rm.Background([grey] * 10 + [yellow] * 30, rm.BG_FPS)  # the picture turns yellow after two seconds
    words = [
        Word(*w) for w in fc._seq(0.2, 0.3, "one two three four five six seven eight nine ten eleven twelve")
    ]
    props, _ = rm.build_props(words, 6.0, "punch", box=box, fps=30, colors="auto", background=bg)
    P = punch()
    early = [p for p in props["pages"] if p["end"] <= 1.9]
    late = [p for p in props["pages"] if p["start"] >= 2.2]
    assert early and late
    assert all(p["colors"]["active"] == P["colors"]["active"] for p in early)  # yellow highlight on grey
    assert all(p["colors"]["active"] != P["colors"]["active"] for p in late)  # not on a yellow picture
    assert len({p["colors"]["active"] for p in late}) == 1  # and it stays put
    changed = [
        p for a, p in zip(props["pages"], props["pages"][1:], strict=False) if a["colors"] != p["colors"]
    ]
    assert changed and all(p["from"] is not None for p in changed)  # the change is eased, not cut
    fixed, _ = rm.build_props(words, 6.0, "punch", box=box, fps=30, colors="fixed", background=bg)
    assert all(p["colors"] == P["colors"] for p in fixed["pages"])  # fixed mode ignores the picture


def test_template_parameters_can_be_overridden_from_the_config(monkeypatch):
    base = config.global_config()
    over = {
        "templates": {"punch": {"fx": {"activeScale": 1.3}, "paging": {"maxWords": 2}}},
        "colors": "fixed",
    }
    monkeypatch.setattr(
        config, "global_config", lambda: {**base, "render": {**base.get("render", {}), "rm": over}}
    )
    P = rm.load_template("punch")
    assert (
        P["fx"]["activeScale"] == 1.3
        and P["paging"]["maxWords"] == 2
        and P["fx"]["halo"] == raw_punch()["fx"]["halo"]
    )
    assert rm.color_mode() == "fixed"
    pl = plan_of([Word(*w) for w in fc._seq(0.1, 0.3, "a b c d")], 3.0, P)
    assert max(len(p.words) for p in pl.pages) == 2


def test_the_animation_library_is_known_by_name_and_slot():
    lib = rm.animations()
    assert len(lib) >= 20 and set(lib.values()) == set(rm.SLOTS)
    assert (
        lib["pop"] == "enter"
        and lib["shrink"] == "exit"
        and lib["punch"] == "active"
        and lib["shake"] == "emph"
    )
    for slot in rm.SLOTS:
        assert sum(1 for v in lib.values() if v == slot) >= 3, slot
    # punch names an animation of the right slot in every slot it fills
    fx = raw_punch()["fx"]
    for slot in rm.SLOTS:
        assert fx[slot] is None or lib[fx[slot]["name"]] == slot


def test_an_animation_is_swapped_whole_from_the_settings(monkeypatch):
    P = raw_punch()
    swapped = rm.with_animation(P, "enter", "drop", "letter")
    assert swapped["fx"]["enter"] == {"name": "drop", "params": {}, "by": "letter"}
    assert (
        swapped["fx"]["exit"] == P["fx"]["exit"] and P["fx"]["enter"]["name"] == "pop"
    )  # nothing else, no side effect
    assert rm.with_animation(P, "idle", "float")["fx"]["idle"] == {"name": "float", "params": {}}
    with pytest.raises(rm.RemotionError):
        rm.with_animation(P, "enter", "shrink")  # an exit is not an entrance
    with pytest.raises(rm.RemotionError):
        rm.with_animation(P, "enter", "slam", "word")  # a slam is for the whole block
    units = rm.animation_units()
    assert (
        units["slam"] == ("page",) and "letter" in units["pop"] and all("page" in u for u in units.values())
    )
    assert set(units) == {n for n, s in rm.animations().items() if s in ("enter", "exit")}
    # the same through the config: pop's parameters must not leak into drop, drop's own are kept
    base = config.global_config()
    over = {
        "templates": {
            "punch": {
                "fx": {
                    "enter": {"name": "drop", "params": {"height": 1.2}},
                    "active": {"params": {"scale": 1.3}},
                }
            }
        }
    }
    monkeypatch.setattr(
        config, "global_config", lambda: {**base, "render": {**base.get("render", {}), "rm": over}}
    )
    got = rm.load_template("punch")["fx"]
    assert got["enter"] == {"name": "drop", "params": {"height": 1.2}}  # by: the renderer's default, page
    assert got["active"] == {
        "name": "punch",
        "params": {**P["fx"]["active"]["params"], "scale": 1.3},
    }  # same name: merged


def test_an_own_template_is_its_base_as_it_shipped_with_its_settings(monkeypatch):
    base, P = config.global_config(), raw_punch()
    over = {
        "templates": {"punch": {"size": 100}},  # this machine's edit of the base: not under an own one
        "presets": {
            "loud": {"name": "Loud", "base": "punch", "settings": {"size": 130}, "created": 5},
            "plain": {"name": "Plain", "base": "punch"},
            "punch": {"name": "Shadowed", "base": "jelly"},  # a shipped template's name: never an id
            "Not An Id": {"name": "x", "base": "punch"},
            "orphan": {"name": "x", "base": "gone"},
        },
    }
    monkeypatch.setattr(
        config, "global_config", lambda: {**base, "render": {**base.get("render", {}), "rm": over}}
    )
    assert rm.presets() == {
        "loud": {"name": "Loud", "base": "punch", "settings": {"size": 130}, "created": 5.0},
        "plain": {"name": "Plain", "base": "punch", "settings": {}, "created": 0.0},
    }
    assert "loud" not in rm.templates()  # the shipped ones, which the frame checks go through
    assert rm.load_template("loud") == {**P, "size": 130} and rm.load_template("plain") == P
    assert rm.load_template("punch")["size"] == 100 and rm.load_template("punch")["title"] == "Punch"
    assert rm.load_template("loud", {"tilt": 2})["tilt"] == 2
    assert rm.base_name("loud") == "punch" and rm.base_name("punch") == "punch"
    with rm.overriding({"templates": {"loud": {"size": 70}}}):  # one video's own settings
        assert rm.load_template("loud")["size"] == 70
    # the renderer draws the base's component with the own template's parameters
    words = [Word(0.2, 0.6, "hello"), Word(0.6, 1.0, "there")]
    props, _ = rm.build_props(words, 2.0, "loud", box=rm.caption_box(960, "seam"), fps=30, colors="fixed")
    assert props["template"] == "punch" and props["params"]["size"] == 130
    for gone in ("orphan", "nope"):
        with pytest.raises(rm.RemotionError, match="unknown caption template"):
            rm.load_template(gone)


def test_a_short_falls_back_when_its_own_template_is_gone(monkeypatch, tmp_path):
    """A style that names an own template nobody has any more is a template that cannot be rendered: the
    clip gets the built-in style and says why."""
    calls = []
    monkeypatch.setattr(rm, "missing", lambda: None)
    monkeypatch.setattr(rm, "color_mode", lambda: "fixed")
    box = rm.caption_box(960, "seam")
    used = template._encode_with_captions(
        lambda style, overlay: calls.append((style, overlay)),
        "rm:gone_one", tmp_path, [Word(0.2, 0.6, "hello")], 2.0, 30.0, box, None, None, lambda p: ["true"],
    )  # fmt: skip
    assert calls == [("word", None)] and used["captions"] == "word"
    assert "unknown caption template 'gone_one'" in used["captions_error"]


def test_unknown_templates_and_modes_are_errors_not_crashes():
    with pytest.raises(rm.RemotionError):
        rm.load_template("no-such-template")
    with pytest.raises(rm.RemotionError):
        rm.load_template("../punch")
    with pytest.raises(rm.RemotionError):
        rm.build_props([], 1.0, "punch", box=rm.caption_box(960, "seam"), fps=30, colors="rainbow")
    assert "_probe" not in rm.templates()  # names starting with _ are test fixtures, not caption styles


# ------------------------------------------------------------------ style selection and fallback


def test_rm_styles_are_selected_from_the_config(monkeypatch):
    base = config.global_config()

    def with_captions(v, rm_cfg=None):
        render = {**base.get("render", {}), "captions": v, "rm": rm_cfg or {}}
        monkeypatch.setattr(config, "global_config", lambda: {**base, "render": render})

    with_captions("rm:punch")
    assert (
        captions.caption_style("x") == "rm:punch"
        and rm.is_rm("rm:punch")
        and rm.template_name("rm:punch") == "punch"
    )
    with_captions("rm:")
    assert captions.caption_style("x") == "word"
    with_captions("pop")
    assert captions.caption_style("x") == "pop" and not rm.is_rm("pop")
    with_captions("rm:punch", {"fallback": "pop"})
    assert rm.fallback_style() == "pop"
    with_captions("rm:punch", {"fallback": "rm:slam"})
    assert rm.fallback_style() == "word"  # the fallback is always an ASS style


def test_missing_setup_is_reported_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(rm, "node_bin", lambda: None)
    assert rm.available() is False and "node" in rm.missing()
    with pytest.raises(rm.RemotionError):
        rm.render_overlay(
            [Word(0, 1, "hi")], 2.0, "punch", tmp_path / "o", box=rm.caption_box(960, "seam"), fps=30
        )


def test_a_failing_overlay_never_costs_the_clip(monkeypatch, tmp_path, caplog):
    """Node missing, a broken render, a crash in the planner, ffmpeg choking on the overlay: ASS captions."""
    calls = []

    def encode(style, overlay):
        calls.append((style, overlay))
        if overlay is not None:
            raise RuntimeError("ffmpeg failed (1): overlay")

    box = rm.caption_box(960, "seam")
    args = (tmp_path, [Word(0, 1, "hi")], 2.0, 30.0, box, None, None, lambda p: ["true"])
    monkeypatch.setattr(rm, "color_mode", lambda: "fixed")
    with caplog.at_level(logging.WARNING):
        monkeypatch.setattr(rm, "missing", lambda: "node not found")
        template._encode_with_captions(encode, "rm:punch", *args)
        assert calls == [("word", None)] and "node not found" in caplog.text

        calls.clear()
        monkeypatch.setattr(rm, "render_overlay", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
        template._encode_with_captions(encode, "rm:punch", *args)
        assert calls == [("word", None)]

        calls.clear()
        fake = rm.Overlay(
            dir=tmp_path, pattern="%06d.png", fps=30, frames=60, width=1080, height=256, x=0, y=770
        )
        monkeypatch.setattr(rm, "render_overlay", lambda *a, **k: fake)
        template._encode_with_captions(encode, "rm:punch", *args)
        assert calls == [
            ("rm:punch", fake),
            ("word", None),
        ]  # the encode with the overlay failed: once more with ASS

        calls.clear()
        monkeypatch.setattr(rm, "render_overlay", lambda *a, **k: None)  # nothing to show
        template._encode_with_captions(encode, "rm:punch", *args)
        assert calls == [("rm:punch", None)]

        calls.clear()
        template._encode_with_captions(encode, "pop", *args)  # ASS styles do not go near Remotion
        assert calls == [("pop", None)]


def test_graphs_take_the_overlay_after_the_credit_and_the_probe_instead_of_captions(tmp_path):
    ov = rm.Overlay(dir=tmp_path, pattern="%06d.png", fps=60, frames=120, width=1080, height=256, x=0, y=770)
    kw = dict(
        trim=6.0, dur=4.0, has_webcam=True, cam=(40, 700, 560, 250), game=(0, 0, 1920, 1080), logo_idx=1,
        ass=tmp_path / "s.ass", fontsdir=tmp_path,
    )  # fmt: skip
    plain = template.build_split_graph(**kw)
    with_ov = template.build_split_graph(**kw, overlay=(2, ov))
    assert "overlay=x=0:y=770" not in plain and plain.count("[v]") == 1
    assert "ass=filename=" in with_ov and "[2:v]settb=intb/2,setpts=PTS-1" in with_ov
    assert "crop=1080:256:0:770" in with_ov and with_ov.index("ass=filename=") < with_ov.index("[2:v]")
    assert with_ov.count("[a]") == plain.count("[a]") == 1
    probe = template.build_split_graph(**kw, probe=rm.probe_filter(rm.caption_box(960, "seam")))
    assert "ass=" not in probe and "[a]" not in probe and probe.endswith("format=rgb24[v]")
    stack = dict(
        trim=6.0, dur=4.0, style="ambient", has_webcam=True, has_hook=False, cam=(40, 700, 560, 250),
        game=(0, 0, 1920, 1080), gradient_idx=-1, logo_idx=1, hook_idx=-1, ass=tmp_path / "s.ass",
        fontsdir=tmp_path,
    )  # fmt: skip
    assert "[rmdone]" in template.build_short_graph(**stack, overlay=(2, ov))
    assert "[a]" not in template.build_short_graph(**stack, probe="null")


@pytest.mark.ffmpeg
def test_render_short_with_an_rm_style_falls_back_to_ass_when_remotion_is_missing(
    synth_src, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(rm, "missing", lambda: "Node dependencies not installed (run `npm ci` in remotion/)")
    words = captions.demo_words(4.0)
    out = {}
    with caplog.at_level(logging.WARNING):
        for style in ("rm:punch", "word"):
            out[style] = template.render_short(
                synth_src, 8.0, 4.0, tmp_path / f"{style[:2]}.mp4", clip_id="clip-1", streamer="tester",
                words=words,
                hook=None, profile="cpu_live", captions=style, layout="split",
            )  # fmt: skip
    assert "using the ASS style word" in caplog.text
    a, b = out["rm:punch"].read_bytes(), out["word"].read_bytes()
    assert len(a) > 50_000 and abs(len(a) - len(b)) < 0.02 * len(b)  # the same clip as the plain ASS render
