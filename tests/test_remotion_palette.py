"""Caption colours: contrast maths against published reference values, the palette, the auto mode.

Pure Python: these always run (no Node, no ffmpeg, no numpy)."""

from __future__ import annotations

import itertools
import json
import random

import pytest

from clipwatcher.render import palette as pal
from clipwatcher.render import remotion as rm


def test_wcag_contrast_reference_values():
    assert pal.contrast("#FFFFFF", "#000000") == pytest.approx(21.0)
    assert pal.contrast("#000000", "#FFFFFF") == pytest.approx(21.0)  # order does not matter
    assert pal.luminance("#FFFFFF") == pytest.approx(1.0) and pal.luminance("#000000") == 0.0
    # the textbook pair: #777 on white misses AA, #767676 is the darkest grey that makes it
    assert pal.contrast("#777777", "#FFFFFF") == pytest.approx(4.478, abs=0.001)
    assert pal.contrast("#767676", "#FFFFFF") == pytest.approx(4.542, abs=0.001)
    assert pal.contrast("#FF0000", "#FFFFFF") == pytest.approx(3.998, abs=0.001)
    assert pal.luminance("#808080") == pytest.approx(0.2159, abs=0.0001)


def test_contrast_is_never_rounded_up():
    # 4.478:1 is not 4.5:1 (Understanding SC 1.4.3: "4.499:1 would not meet the 4.5:1 threshold")
    assert pal.check_scheme({"text": "#777777", "stroke": "#FFFFFF"}, [("text", "stroke")])
    assert not pal.check_scheme({"text": "#767676", "stroke": "#FFFFFF"}, [("text", "stroke")])


@pytest.mark.parametrize(
    "text,bg,lc",
    [  # the APCA-W3 0.0.98G-4g test values
        ("#888888", "#ffffff", 63.056469930209424),
        ("#ffffff", "#888888", -68.54146436644962),
        ("#000000", "#aaaaaa", 58.146262578561334),
        ("#aaaaaa", "#000000", -56.24113336839742),
        ("#112233", "#ddeeff", 91.66830811481631),
        ("#ddeeff", "#112233", -93.06770049484275),
    ],
)
def test_apca_reference_values(text, bg, lc):
    assert pal.apca(text, bg) == pytest.approx(lc, abs=1e-9)


def test_oklch_of_the_primaries():
    for hexv, want in (
        ("#FF0000", (0.628, 0.2577, 29.23)),
        ("#00FF00", (0.8664, 0.2948, 142.5)),
        ("#0000FF", (0.452, 0.3132, 264.05)),
    ):
        got = pal.oklch(hexv)
        assert got == pytest.approx(want, abs=0.01)
    assert pal.hue_distance(350, 10) == pytest.approx(20) and pal.hue_distance(10, 350) == pytest.approx(20)
    assert pal.hue_distance(0, 180) == pytest.approx(180)


def test_mix_endpoints_and_roundtrip():
    assert pal.mix("#FFFFFF", "#FFE600", 0) == "#FFFFFF" and pal.mix("#FFFFFF", "#FFE600", 1) == "#FFE600"
    mid = pal.mix("#FFFFFF", "#000000", 0.5)
    r, g, b = pal.parse_hex(mid)
    assert r == g == b and 90 < r < 110  # perceptual middle grey, not the 188 of a linear-light mix
    for name in pal.PALETTE:
        assert pal.to_hex(pal.from_oklab(pal.oklab(pal.PALETTE[name]))) == pal.PALETTE[name]


def test_every_palette_fill_clears_the_target_on_the_inks():
    for light in pal.LIGHT:
        for dark in ("black", "ink"):
            assert pal.contrast(pal.PALETTE[light], pal.PALETTE[dark]) >= pal.TARGET, (light, dark)
        assert abs(pal.apca(pal.PALETTE[light], "#000000")) >= 45, light  # APCA: at least "large text" level


def test_colour_transitions_between_fills_never_dip_under_the_target():
    # the renderer eases colours (white -> yellow on the spoken word, scheme to scheme): every step counts
    for a, b in itertools.combinations(pal.LIGHT, 2):
        assert pal.worst_between(pal.PALETTE[a], pal.PALETTE[b], "#000000") >= pal.TARGET, (a, b)


def _flat(hexv: str, n: int = 400):
    return [pal.parse_hex(hexv)] * n


def test_bg_stats_sees_hue_luminance_and_grey():
    y = pal.bg_stats(_flat("#F2D000"))
    assert y.chromatic == pytest.approx(1.0) and y.share_near(pal.oklch("#FFE600")[2]) == pytest.approx(1.0)
    assert y.lum_lo == pytest.approx(y.lum_hi)
    g = pal.bg_stats(_flat("#808080"))
    assert g.chromatic == 0 and g.hues == []
    half = pal.bg_stats(_flat("#000000", 200) + _flat("#FFFFFF", 200))
    assert half.lum_lo < 0.01 and half.lum_hi > 0.95 and half.samples == 400
    assert pal.bg_stats([]).samples == 0


def test_collision_is_about_hue_not_brightness():
    car = pal.bg_stats(_flat("#E8C400"))  # a yellow car fills the caption box
    assert pal.collides(pal.PALETTE["yellow"], car) >= pal.COLLIDE_SHARE
    assert pal.collides(pal.PALETTE["gold"], car) >= pal.COLLIDE_SHARE  # a neighbouring hue collides too
    assert pal.collides(pal.PALETTE["cyan"], car) == 0 and pal.collides(pal.PALETTE["pink"], car) == 0
    assert pal.collides(pal.PALETTE["white"], car) == 0  # greys have no hue to collide with
    assert pal.collides(pal.PALETTE["yellow"], pal.bg_stats(_flat("#101010"))) == 0


PUNCH = (
    json.loads((rm.TEMPLATES_DIR / "punch.json").read_text())
    if (rm.TEMPLATES_DIR / "punch.json").is_file()
    else None
)
needs_templates = pytest.mark.skipif(PUNCH is None, reason="no remotion/templates")


@needs_templates
def test_auto_keeps_the_designed_scheme_on_a_neutral_picture():
    got = pal.auto_colors(PUNCH["colors"], PUNCH["auto"], pal.bg_stats(_flat("#303030")))
    assert got["active"] == pal.PALETTE["yellow"] and got["text"] == "#FFFFFF" and got["stroke"] == "#000000"
    assert set(got) == set(PUNCH["colors"])  # always a complete scheme


@needs_templates
def test_auto_moves_the_highlight_off_the_hue_under_it():
    got = pal.auto_colors(PUNCH["colors"], PUNCH["auto"], pal.bg_stats(_flat("#E8C400")))
    assert got["active"] != pal.PALETTE["yellow"]
    assert pal.collides(got["active"], pal.bg_stats(_flat("#E8C400"))) < pal.COLLIDE_SHARE
    red = pal.auto_colors(PUNCH["colors"], PUNCH["auto"], pal.bg_stats(_flat("#D02818")))
    assert pal.collides(red["emph"], pal.bg_stats(_flat("#D02818"))) < pal.COLLIDE_SHARE
    assert red["active"] == pal.PALETTE["yellow"]  # nothing wrong with yellow on red: it stays


@needs_templates
def test_auto_roles_stay_apart_and_above_the_floor_on_any_picture():
    rnd = random.Random(3)
    pictures = [_flat(pal.PALETTE[n]) for n in pal.PALETTE]
    pictures += [
        [(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(500)] for _ in range(6)
    ]
    for px in pictures:
        got = pal.auto_colors(PUNCH["colors"], PUNCH["auto"], pal.bg_stats(px))
        assert pal.check_scheme(got, PUNCH["auto"]["pairs"], pal.TARGET) == []
        assert got["active"] != got["emph"]
        assert pal.hue_distance(pal.oklch(got["active"])[2], pal.oklch(got["emph"])[2]) >= pal.HUE_APART


@needs_templates
def test_auto_does_not_flicker_between_pages():
    state = pal.AutoState()
    calm, car = pal.bg_stats(_flat("#203040")), pal.bg_stats(_flat("#E8C400"))
    seq = []
    for i in range(12):  # the picture changes a little every page, never enough to collide
        st = pal.bg_stats(_flat("#203040", 300) + _flat("#3A6B2A" if i % 2 else "#274A7A", 60))
        seq.append(pal.auto_colors(PUNCH["colors"], PUNCH["auto"], st, state, now=i * 0.8)["active"])
    assert len(set(seq)) == 1
    moved = pal.auto_colors(PUNCH["colors"], PUNCH["auto"], car, state, now=10.0)["active"]
    assert moved != seq[0]  # a real collision changes the colour at once
    back = pal.auto_colors(PUNCH["colors"], PUNCH["auto"], calm, state, now=10.6)["active"]
    assert back == moved  # 0.6 s later the car is gone: the new colour stays (no flip-flop)
    later = pal.auto_colors(PUNCH["colors"], PUNCH["auto"], calm, state, now=10.0 + pal.DWELL_S + 5)["active"]
    assert later in (moved, seq[0])


def test_transitions_that_would_cross_the_floor_are_refused():
    a = {"plate": "#000000", "plateText": "#FFFFFF", "text": "#FFFFFF", "stroke": "#000000"}
    b = {"plate": "#FFE600", "plateText": "#000000", "text": "#FFE600", "stroke": "#000000"}
    assert not pal.safe_transition(
        a, b, [("plateText", "plate")]
    )  # plate and text swap brightness: they meet
    assert pal.safe_transition(a, b, [("text", "stroke")])  # white to yellow on black never comes close


def _templates():
    return [(n, json.loads((rm.TEMPLATES_DIR / f"{n}.json").read_text())) for n in rm.templates()]


@pytest.mark.parametrize(
    "name,params", _templates() or [pytest.param("", {}, marks=pytest.mark.skip(reason="no templates"))]
)
def test_every_template_ships_colours_that_meet_the_target(name, params):
    """The fixed scheme and every scheme the auto mode can arrive at: all adjacent roles at 7:1 or more."""
    pairs = params["auto"]["pairs"]
    assert pairs, "a template must say which roles sit next to each other"
    assert pal.check_scheme(params["colors"], pairs, pal.TARGET) == [], name
    for role, cands in params["auto"]["candidates"].items():
        assert role in pal.ROLES or role == "cycle", role
        for c in cands:
            pal.resolve(c)  # a palette name or a hex value
    rnd = random.Random(11)
    pictures = [_flat(pal.PALETTE[n]) for n in pal.PALETTE]
    pictures += [
        [(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(300)] for _ in range(4)
    ]
    for px in pictures:
        got = pal.auto_colors(params["colors"], params["auto"], pal.bg_stats(px))
        assert pal.check_scheme(got, pairs, pal.TARGET) == [], name
