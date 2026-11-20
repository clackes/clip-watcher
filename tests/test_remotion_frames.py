"""Frame-by-frame tests of the Remotion caption templates (render/framecheck.py).

The first half tests the checks themselves on made-up scene records: no Node needed, always runs. The
second half renders every frame of every template at 30 and 60 fps over synthetic fixtures and backgrounds
(marker `remotion`; skipped unless `npm ci && npm run setup` was run in remotion/, and without numpy).
CLIPWATCHER_FRAMECHECK=full runs the whole fixture set (minutes per template) instead of the quick one.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess

import pytest

from clipwatcher.models import Word
from clipwatcher.render import framecheck as fc
from clipwatcher.render import remotion as rm
from clipwatcher.render import rm_plan

HAVE_TEMPLATES = (rm.TEMPLATES_DIR / "punch.json").is_file()


def _numpy():
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def render_test(fn):
    """A test that renders: marked `remotion`, skipped without the Node setup or numpy."""
    fn = pytest.mark.skipif(not _numpy(), reason="numpy not installed")(fn)
    fn = pytest.mark.skipif(rm.missing() is not None, reason=f"Remotion not set up: {rm.missing()}")(fn)
    return pytest.mark.remotion(fn)


# ------------------------------------------------------------------ the checks catch what they should


def _ramp(x: float) -> float:
    return min(1.0, max(0.0, x))


def _plan_and_frames(fps: float = 30.0, shift: float = 0.0, grow=None):
    """A plan and the scene records a correct renderer would report for it (boxes made up, timing exact).
    shift: the words that much later; grow(seconds since the word began) -> how much bigger the spoken word
    is (default: a tenth of a second up to 12 %)."""
    P = rm.load_template("punch")
    said = [*fc._seq(0.2, 0.3, "one two three"), (2.0, 2.4, "four")]
    words = [Word(a + shift, b + shift, t) for a, b, t in said]
    grow = grow or (lambda since: 0.12 * _ramp(since / 0.1))
    pl = rm_plan.plan(words, 3.0, P, box_w=900, box_h=190)
    n = round(3.0 * fps)
    frames = {}
    for f in range(n):
        T = (f + 0.5) / fps
        units = []
        for p in pl.pages:
            if not (p.start <= T < p.end):
                continue
            for i, w in enumerate(p.words):
                units.append(
                    {
                        "id": f"p{p.id}w{i}", "p": p.id, "w": i, "c": -1, "lb": 0, "tx": w.text, "e": 0,
                        "em": 0,
                        "a": 1 if w.start <= T < w.active_end else 0, "m": [1, 0, 0, 1, 0, 0],
                        "fg": ["#FFFFFF"],
                        # the spoken word grows, and eases out over a tenth of a second
                        "sc": 1 + grow(T - w.start) * _ramp((w.active_end + 0.1 - T) / 0.1),
                        "rot": 0.0, "z": 0, "box": [200.0 + 220 * i, 100.0, 180.0, 70.0], "clip": None,
                        "halo": 9.0, "reach": 12.0,
                    }
                )  # fmt: skip
        frames[f] = {"f": f, "t": T, "u": units, "s": []}
    return P, pl, frames, n, fps


def _result():
    return fc.CaseResult("punch", "synthetic", 30.0, "fixed")


@pytest.mark.skipif(not HAVE_TEMPLATES, reason="no remotion/templates")
def test_a_correct_scene_passes_the_plan_and_continuity_checks():
    P, pl, frames, n, fps = _plan_and_frames()
    res = _result()
    fc.check_plan_frames(res, pl, P, frames, n, fps)
    fc.check_highlight(res, pl, frames, fps)
    fc.check_continuity(res, frames, n, fps)
    assert res.ok, res.summary()
    assert res.checks["active_timing"] == 4 and res.checks["visible_set"] == n and res.checks["jump"] > 100

    plain = copy.deepcopy(frames)
    for rec in plain.values():
        for u in rec["u"]:
            u["sc"] = 1.0  # a highlight nobody can see is no highlight
    res = _result()
    fc.check_highlight(res, pl, plain, fps)
    assert res.fail_count["highlight"] == 3


@pytest.mark.skipif(not HAVE_TEMPLATES, reason="no remotion/templates")
def test_late_highlights_are_caught_but_one_frame_is_allowed():
    P, pl, frames, n, fps = _plan_and_frames()
    for shift, bad in ((1, False), (3, True)):
        late = copy.deepcopy(frames)
        for f in range(n - 1, -1, -1):  # every highlight `shift` frames late
            src = frames.get(f - shift, {"u": []})
            flags = {u["id"]: u["a"] for u in src["u"]}
            for u in late[f]["u"]:
                u["a"] = flags.get(u["id"], 0)
        res = _result()
        fc.check_plan_frames(res, pl, P, late, n, fps)
        assert bool(res.fail_count.get("active_timing")) is bad, res.summary()


@pytest.mark.skipif(not HAVE_TEMPLATES, reason="no remotion/templates")
def test_leftovers_missing_words_and_single_frame_units_are_caught():
    P, pl, frames, n, fps = _plan_and_frames()
    last = pl.pages[0]
    end = round(last.end * fps)
    stale = copy.deepcopy(frames)
    stale[end + 4]["u"] = copy.deepcopy(frames[end - 3]["u"])  # the first page shows up again after it left
    res = _result()
    fc.check_plan_frames(res, pl, P, stale, n, fps)
    assert res.fail_count["visible_set"] == 1 and res.fail_count["lifetime"] >= 1

    gone = copy.deepcopy(frames)
    mid = round((last.start + last.end) / 2 * fps)
    gone[mid]["u"] = [u for u in gone[mid]["u"] if u["w"] != 1]  # one word blinks off for a frame
    res = _result()
    fc.check_plan_frames(res, pl, P, gone, n, fps)
    assert res.fail_count["words_shown"] == 1 and res.fail_count["lifetime"] == 1

    never = copy.deepcopy(frames)
    for rec in never.values():
        for u in rec["u"]:
            if u["p"] == 0 and u["w"] == 2:
                u["a"] = 0  # the third word is never highlighted
    res = _result()
    fc.check_plan_frames(res, pl, P, never, n, fps)
    assert res.fail_count["active_timing"] == 1


@pytest.mark.skipif(not HAVE_TEMPLATES, reason="no remotion/templates")
def test_jumps_and_flashes_are_caught_and_smooth_motion_is_not():
    P, pl, frames, n, fps = _plan_and_frames()
    page = pl.pages[0]
    a = round(page.start * fps) + 2

    def moved(fn):
        out = copy.deepcopy(frames)
        for f in range(a, n):
            for u in out[f]["u"]:
                if u["w"] == 0:
                    fn(u, f - a)
        res = _result()
        fc.check_continuity(res, out, n, fps)
        return res

    def glide(u, k):  # an ease-out slide and a colour change over four frames: fine
        u["box"][0] += 60 * (1 - 0.6**k)
        u["fg"] = [f"#FF{max(0, 255 - 60 * min(k, 4)):02X}{max(0, 255 - 60 * min(k, 4)):02X}"]

    assert moved(glide).ok
    teleport = moved(lambda u, k: u["box"].__setitem__(0, u["box"][0] + (80 if k >= 5 else 0)))
    assert teleport.fail_count["jump"] >= 1
    blink = moved(lambda u, k: u.__setitem__("sc", 1.4 if k == 5 else 1.0))
    assert blink.fail_count["flash"] >= 1
    snap = moved(lambda u, k: u.__setitem__("fg", ["#FFE600" if k >= 5 else "#FFFFFF"]))
    assert snap.fail_count["jump"] >= 1  # a colour that changes in a single frame
    buzz = moved(lambda u, k: u.__setitem__("rot", 6.0 if k % 2 else -6.0))
    assert buzz.fail_count["flash"] >= 3  # 15 Hz at 30 fps is flicker, not a shake


def test_a_fixture_moved_by_part_of_a_frame_is_the_same_talk():
    fx = fc.FIXTURES["mix"]
    assert fx.shifted(0) is fx
    moved, d = fx.shifted(0.25), 0.25 / 30
    assert moved.name == "mix+0.25" and moved.box == fx.box
    assert moved.dur == pytest.approx(fx.dur + 1 / 30, abs=1e-4)  # nothing is cut off at the end
    for (s0, e0, t0), (s1, e1, t1) in zip(fx.words, moved.words, strict=True):
        assert t0 == t1 and s1 - s0 == pytest.approx(d, abs=1e-4) and e1 - e0 == pytest.approx(d, abs=1e-4)
    assert (
        moved.beats[0][0] - fx.beats[0][0] == pytest.approx(d, abs=1e-4)
        and moved.beats[0][1:] == fx.beats[0][1:]
    )
    assert moved.actions[0][1] - fx.actions[0][1] == pytest.approx(d, abs=1e-4)
    assert moved.actions[0][2] == fx.actions[0][2]
    assert fc.parse_phases("4") == (0.0, 0.25, 0.5, 0.75) and fc.parse_phases("0,0.3") == (0.0, 0.3)
    with pytest.raises(ValueError):
        fc.parse_phases("0,1.5")
    assert set(fc.SWEEP) <= set(fc.FULL) and fc.PHASES[0] == 0 and len(fc.PHASES) >= 4


@pytest.mark.skipif(not HAVE_TEMPLATES, reason="no remotion/templates")
def test_a_punch_that_fits_into_one_frame_is_caught_at_some_phases_only():
    """Why the fixtures are swept through the frame: a pop that is at full size after a third of a tenth of
    a second is spread over two frames when the word starts in the middle of a frame, and is all in one
    frame when it starts just before a frame's instant. One phase says nothing about the others."""

    def back(k):
        return lambda x: 1 + (k + 1) * (x - 1) ** 3 + k * (x - 1) ** 2

    def jumps(seconds: float, phase: float) -> int:
        grow = lambda since: 0.2 * back(2.4)(_ramp(since / seconds))  # noqa: E731
        _, _, frames, n, fps = _plan_and_frames(shift=phase / 30, grow=grow)
        res = _result()
        fc.check_continuity(res, frames, n, fps)
        assert not res.fail_count.get("flash"), res.summary()
        return res.fail_count.get("jump", 0)

    phases = [i / 16 for i in range(16)]
    snappy = [jumps(0.1, ph) for ph in phases]
    assert any(snappy) and not all(snappy), snappy  # a true failure, and one that a single phase can miss
    assert not any(jumps(0.16, ph) for ph in phases)  # the same pop given its time: fine wherever it starts


def test_adversarial_backgrounds_are_deterministic_and_cover_the_cases():
    if not _numpy():
        pytest.skip("numpy not installed")
    for kind in (*fc.SYNTH, "blocks", "#FFE600"):
        a, b = fc.background(kind, 7, 20, 30, 4, 6), fc.background(kind, 7, 20, 30, 4, 6)
        assert a.shape == (20, 30, 3) and (a == b).all()
    assert (fc.background("flash", 0, 2, 2) == 255).all() and (fc.background("flash", 2, 2, 2) == 0).all()
    assert len({tuple(p) for p in fc.background("noise", 1, 16, 16).reshape(-1, 3)}) > 200


# ------------------------------------------------------------------ every frame of every template


def test_a_unit_scaled_to_nothing_that_leaves_is_not_a_stutter():
    unit = {"id": "p0w0c0", "fg": ["#FFFFFF"], "box": [540.0, 272.0, 49.0, 49.0], "sc": 1.0}
    gone = {"id": "p0w0c1", "fg": ["#FFFFFF"], "box": [512.0, 350.4, 0.0, 0.0], "sc": 0.0}
    assert not fc._moved({"u": [unit, gone]}, {"u": [unit]})  # nothing of it was drawn
    small = {**gone, "box": [512.0, 350.4, 6.0, 6.0], "sc": 0.12}
    assert fc._moved({"u": [unit, small]}, {"u": [unit]})  # a letter still on screen that vanishes
    assert fc._moved({"u": [unit]}, {"u": [{**unit, "box": [541.0, 272.0, 49.0, 49.0]}]})
    assert not fc._moved({"u": [unit]}, {"u": [dict(unit)]})


def test_a_unit_its_window_shows_nothing_of_is_not_a_stutter_when_it_leaves():
    """marker at one phase of the sweep: a word the exit had wiped out was still in the scene for a frame,
    its window ending 26 px short of its letters, and left in the next. No pixel changed: none was drawn."""
    stays = {"id": "p0w1", "fg": ["#FFFFFF"], "box": [100.0, 250.0, 200.0, 70.0], "sc": 1.0, "clip": None}
    wiped = {
        "id": "p1w0", "fg": ["#FFFFFF"], "box": [421.03, 255.87, 236.35, 70.67], "sc": 1.0,
        "m": [1, 0, 0, 1, 0, 0], "reach": 12.0, "clip": [60, 110, 335.41, 256],
    }  # fmt: skip
    assert not fc._moved({"u": [stays, wiped]}, {"u": [stays]})
    # the window ends 9 px short of the letters: the outline reaches 12 px, so some of it was on screen
    assert fc._moved({"u": [stays, {**wiped, "clip": [60, 110, 352.0, 256]}]}, {"u": [stays]})
    assert fc._moved(
        {"u": [stays, {**wiped, "clip": [60, 110, 480.0, 256]}]}, {"u": [stays]}
    )  # half the word
    assert fc._moved({"u": [stays, {**wiped, "clip": None}]}, {"u": [stays]})  # no window at all


def test_a_sliver_of_the_letter_itself_is_not_what_it_is_read_against(monkeypatch):
    """A box sliding over a white E left 2 px of its stem for one frame (glide, on a real clip): shades of
    white, part of the letter. A light area beside the letter is still a contrast failure."""
    np = pytest.importorskip("numpy")
    img = np.zeros((94, 130, 4), np.uint8)
    img[..., 3] = 255  # black and opaque: the outline and the box's rim
    for top in (20, 41, 62):  # the three arms of the E, eleven rows each, ten rows between them
        img[top : top + 11, 51:81, :3] = 255
    img[20:73, 50, :3] = 215  # what the rim left of the stem: one column a shade of white ...
    for top in (31, 52):
        img[top : top + 10, 51, :3] = 188  # ... and, between the arms, the stem's own soft edge
    u = {"box": [50.0, 20.0, 31.0, 53.0], "fg": ["#FFFFFF"], "halo": 6.7, "sc": 1, "clip": None}
    u["m"] = [1, 0, 0, 1, 0, 0]

    def measured(picture):
        (y0, y1, x0, x1), core, ring = fc.unit_masks(np, picture, u)
        return fc.masks_contrast(np, picture[y0:y1, x0:x1], None, core, ring)

    assert measured(img) > 15
    plate = img.copy()
    plate[20:73, 42:51, :3] = 215  # nine pixels of the same light grey: something else beside the letter
    assert measured(plate) < fc.palette.FLOOR
    # without the rule the sliver reads as a light neighbour: this is the case it is there for
    monkeypatch.setattr(fc, "_slivers", lambda np, ov, pts, opaque, alike: np.zeros(ov.shape[:2], bool))
    assert measured(img) < fc.palette.FLOOR


def test_specks_are_only_what_ink_walls_in():
    np = pytest.importorskip("numpy")

    def drawn(*holes):
        a = np.zeros((90, 220), np.uint8)
        a[20:70, 30:190] = 255  # a plate, like a chip: nothing but its edge
        for y0, y1, x0, x1 in holes:
            a[y0:y1, x0:x1] = 0
        return a

    assert fc.pinholes(np, drawn()) == 0  # the clear pixels around a compact shape are not walled in
    assert fc.pinholes(np, drawn((30, 50, 60, 62))) == 40  # a slit 2 px wide inside it is
    assert fc.pinholes(np, drawn((30, 50, 60, 70))) == 0  # a counter 10 px across is a counter
    assert fc.pinholes(np, drawn((20, 50, 60, 62))) == 0  # a 2 px channel open at the top is not walled in
    # the outside reaches round corners: a channel that turns twice before it ends
    assert fc.pinholes(np, drawn((20, 40, 60, 62), (38, 40, 60, 120), (38, 60, 118, 120))) == 0
    reached = fc._reached(np, drawn((30, 50, 60, 62)) < 128)
    assert reached[0, 0] and reached[89, 219] and not reached[40, 60] and not reached[40, 100]


@pytest.mark.skipif(not rm.node_bin(), reason="node not found")
def test_the_renderer_closes_a_walled_in_gap_and_nothing_else(tmp_path):
    """The last step of every frame (remotion/scripts/gaps.mjs, plain Node): what `specks` counts is painted
    in the colour of its walls; openings, channels and every other pixel are left as they are."""
    np = pytest.importorskip("numpy")
    from PIL import Image

    ink = (11, 11, 18, 255)
    img = np.zeros((90, 220, 4), np.uint8)
    img[20:70, 30:190] = ink  # a dark plate
    img[20, 30:190, 3] = 180  # with a soft upper edge
    img[30:50, 60:62, 3] = 0  # a slit 2 px wide inside it: walled in
    img[35, 62, 3] = 200  # the soft edge of the plate beside the slit
    img[30:50, 100:110, 3] = 0  # a counter 10 px across: an opening
    img[20:50, 140:142, 3] = 0  # a channel 2 px wide that is open at the top
    img[40:43, 170:174] = (255, 230, 0, 100)  # something faint (alpha under a half) with ink all round it
    gaps = 20 * 2 + 3 * 4
    src, dst = tmp_path / "drawn.png", tmp_path / "closed.png"
    Image.fromarray(img).save(src)
    script = rm.REMOTION_DIR / "scripts" / "gaps.mjs"

    def close(a, b):
        r = subprocess.run(
            [rm.node_bin(), str(script), str(a), str(b)], capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)["px"]

    assert fc.pinholes(np, img[..., 3]) == gaps
    assert close(src, dst) == gaps
    out = np.asarray(Image.open(dst).convert("RGBA"))
    assert fc.pinholes(np, out[..., 3]) == 0
    changed = (out != img).any(axis=-1)
    assert changed[30:50, 60:62].all() and changed[40:43, 170:174].all() and changed[35, 62]
    assert int(changed.sum()) == gaps + 1  # the gaps and the one soft pixel beside one: nothing else
    assert (
        (out[30:50, 60:62] == ink).all() and (out[40:43, 170:174] == ink).all() and tuple(out[35, 62]) == ink
    )
    assert (out[30:50, 100:110, 3] == 0).all() and (out[20:50, 140:142, 3] == 0).all()
    assert (out[20, 30:140, 3] == 180).all()  # a soft edge that is not beside a gap stays soft
    # a frame without a gap is written as it came, byte for byte
    again = tmp_path / "again.png"
    assert close(dst, again) == 0 and again.read_bytes() == dst.read_bytes()


def test_a_long_gap_with_a_wide_end_is_one_opening():
    """reel, as a word rolls up under its rail: the strip of video between the two was 2 px high at one end
    and had room for the square at the other, 161 px away. The count followed the roomy part for 160 px
    and called the rest specks. A region is an opening as a whole or a speck as a whole."""
    np = pytest.importorskip("numpy")
    a = np.zeros((60, 420), np.uint8)
    a[10:50, 10:410] = 255  # a plate
    a[20:22, 20:400] = 0  # a slit in it, 2 px high and 380 px long
    assert fc.pinholes(np, a) == 2 * 380  # walled in and nowhere 5 px across: a speck, all of it
    a[20:28, 20:30] = 0  # the same slit with a wide end
    assert fc.pinholes(np, a) == 0


def test_the_full_set_holds_the_case_found_on_a_real_clip():
    assert "shouts" in fc.FULL and "shouts" not in fc.QUICK
    said = [w.text for w in fc.FIXTURES["shouts"].word_list()]
    assert said[4:7] == ["wait", "wait", "wait"] and len(said) == 14 and fc.FIXTURES["shouts"].dur > 5


FIXTURES = fc.FULL if os.environ.get("CLIPWATCHER_FRAMECHECK") == "full" else fc.QUICK


@pytest.mark.parametrize(
    "name", rm.templates() or [pytest.param("", marks=pytest.mark.skip(reason="no templates"))]
)
@render_test
def test_every_frame_of_the_template(name, tmp_path):
    """30 and 60 fps, every fixture, every frame: plan, geometry, continuity, fonts, contrast over the
    adversarial backgrounds; the first fixture also twice (identical frames) and through ffmpeg."""
    results = fc.run_template(name, tmp_path, fixtures=FIXTURES)
    bad = [r.summary() for r in results if not r.ok]
    assert not bad, "\n".join(bad)
    assert sum(r.frames for r in results) >= 300 and all(r.total_checks > 0 for r in results)
    assert min(r.stats["contrast_min"] for r in results if r.checks.get("contrast")) >= fc.palette.FLOOR


@render_test
@pytest.mark.parametrize(
    "change,check",
    [
        ({"colors": {"text": "#3A3A3A", "active": "#444444"}}, "contrast"),  # dark grey on a black outline
        (
            {"margin": {"x": 0, "y": 0}, "fx": {"halo": 0.3, "shadowY": 0.3}},
            "safe_area",
        ),  # no room for the outline
        ({"timing": {"activeInS": 0, "activeOutS": 0}}, "jump"),  # the highlight snaps on in one frame
        (
            {"grow": 0, "wordGap": 0.02, "fx": {"active": {"params": {"scale": 1.5}}}},
            "words_clear",
        ),  # words grow into each other
    ],
)
def test_the_checks_catch_a_broken_template(change, check, tmp_path):
    P = rm._merge(rm.load_template("punch"), change)
    res, _ = fc.run_case("punch", "short", 30.0, tmp_path, params=P, own_colours=False)
    assert res.fail_count.get(check), f"{check} did not fire:\n{res.summary()}"


@render_test
def test_a_speck_is_a_failure_when_the_renderer_leaves_it(tmp_path):
    """The frames as the browser draws them have specks (letters of a word as it pops in, two outlines that
    almost meet); the renderer's last step closes every one of them, and `specks` says so either way."""
    kw = {"synth": ("black",), "own_colours": False}
    raw, _ = fc.run_case("punch", "short", 30.0, tmp_path / "raw", raw=True, **kw)
    assert raw.fail_count.get("specks") and raw.stats["speck_px"] > 0, raw.summary()
    res, _ = fc.run_case("punch", "short", 30.0, tmp_path / "closed", **kw)
    assert res.ok and res.checks["specks"] > 50, res.summary()
    assert res.stats["gaps_closed_px"] == raw.stats["speck_px"]
    assert res.stats["gaps_closed_frames"] == raw.stats["speck_frames"]


@pytest.mark.parametrize(
    "name", rm.templates() or [pytest.param("", marks=pytest.mark.skip(reason="no templates"))]
)
@render_test
def test_every_phase_of_the_template(name, tmp_path):
    """The phase sweep, the part that draws nothing: the fast talk of the sweep's fixtures at 30 fps with
    its words moved by quarters of a frame, the plan and every position, scale, angle and colour of every
    frame checked. (With pixels, more phases, more fixtures: framecheck --phases.)"""
    results = fc.run_sweep(name, tmp_path, pixels=False)
    bad = [r.summary() for r in results if not r.ok]
    assert not bad, "\n".join(bad)
    assert len(results) == len(fc.SWEEP) * len(fc.PHASES) and all(r.checks["jump"] > 500 for r in results)


@render_test
def test_the_animation_library_passes_its_sampled_checks():
    """Every animation, sampled as a function (no rendering): parameter bounds, rest states, no
    discontinuity, no jump or flicker at 30 fps, every parameter does something, and inside a slot no two
    animations move alike."""
    rep = rm.check_animations()
    assert rep["ok"], f"{rep['failed']} {rep['tooClose']}"
    assert rep["animations"] == len(rm.animations()) and rep["checks"] > 10_000
    assert sorted(n for names in rep["bySlot"].values() for n in names) == sorted(rm.animations())


ALL_MODES = os.environ.get("CLIPWATCHER_FRAMECHECK") == "full"


@pytest.mark.parametrize(
    "name", sorted(rm.animations()) or [pytest.param("", marks=pytest.mark.skip(reason="no library"))]
)
@render_test
def test_every_frame_of_the_animation(name, tmp_path):
    """Each animation of the library drawn by the template that is built from it, 30 and 60 fps, every
    frame checked like a template. Entrances and exits run in the finest unit they suit (word by word or
    letter by letter where they can; every unit they suit with CLIPWATCHER_FRAMECHECK=full)."""
    slot = rm.animations()[name]
    units = rm.animation_units().get(name, ("page",))  # the units the animation suits
    for by in units if ALL_MODES else units[-1:]:
        P = rm.with_animation(rm.load_template("punch"), slot, name, by)
        if by != "page":  # units that take turns need the time for it
            P["timing"].update({"exitS": 0.34, "enterS": 0.3})
            P["paging"]["holdS"] = 0.62
            P["label"]["exitS"] = 0.3
        pair = {}
        for fps in (30.0, 60.0):
            res, frames = fc.run_case(
                "punch", "mix" if ALL_MODES else "short", fps, tmp_path / f"{by}-{fps:g}", params=P
            )
            assert res.ok, res.summary()
            pair[fps] = (res, frames)
        fc.check_fps_pair(pair[30.0][0], pair[30.0][1], pair[60.0][1], pair[30.0][0].frames)
        assert pair[30.0][0].ok, pair[30.0][0].summary()


@render_test
def test_slow_motion_is_drawn_without_pixel_snapping(tmp_path):
    """A word drifting a tenth of a pixel per frame, turning and growing: its ink must follow the ideal
    path to a small fraction of a pixel. Browser text snaps to whole pixels vertically (0.46 px off, 1 px
    steps); the glyph outlines the Stage draws do not."""
    import numpy as np

    P = rm.load_template("_probe")
    P["fx"].update({"vx": 6, "vy": 6, "rot": 0, "spin": 0, "growth": 0})
    P["paging"].update({"holdS": 0, "lingerS": 0, "leadS": 0})
    box = rm.caption_box(960, "seam")
    props, _ = rm.build_props(
        [Word(0.0, 2.0, "WAVE")], 2.0, "_probe", box=box, fps=60, colors="fixed", params=P, pad=60
    )
    ov = rm.render_props(props, tmp_path / "f", y=0, all_frames=True)
    cx, cy = [], []
    for f in range(ov.frames):
        img = fc.load_frame(ov.dir, f).astype(np.float64)
        w = img[..., 0] * img[..., 3]  # the white fill, not the black outline
        ys, xs = np.mgrid[0 : img.shape[0], 0 : img.shape[1]]
        cx.append((w * xs).sum() / w.sum())
        cy.append((w * ys).sum() / w.sum())
    t = (np.arange(ov.frames) + 0.5) / 60
    for name, v in (("x", np.array(cx)), ("y", np.array(cy))):
        speed, start = np.polyfit(t, v, 1)
        off = np.abs(v - (speed * t + start)).max()
        steps = np.diff(v)
        assert speed == pytest.approx(6.0, abs=0.05), name
        assert off < 0.12, f"{name}: {off:.3f} px off the straight line"
        assert steps.min() > -0.02 and steps.max() < 0.25, (
            f"{name}: steps {steps.min():.3f}..{steps.max():.3f}, ideal 0.1"
        )


@render_test
def test_the_bundle_is_built_once_and_reused():
    first = rm.ensure_bundle()
    stamp = (first / "clipwatcher-bundle.json").stat().st_mtime_ns
    assert rm.ensure_bundle() == first and (first / "clipwatcher-bundle.json").stat().st_mtime_ns == stamp
    assert rm.bundle_stamp() == rm.source_stamp()
