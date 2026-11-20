"""Frame-by-frame tests of the Remotion caption templates (render/framecheck.py).

The first half tests the checks themselves on made-up scene records: no Node needed, always runs. The
second half renders every frame of every template at 30 and 60 fps over synthetic fixtures and backgrounds
(marker `remotion`; skipped unless `npm ci && npm run setup` was run in remotion/, and without numpy).
CLIPWATCHER_FRAMECHECK=full runs the whole fixture set (minutes per template) instead of the quick one.
"""

from __future__ import annotations

import copy
import os

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


def _plan_and_frames(fps: float = 30.0):
    """A plan and the scene records a correct renderer would report for it (boxes made up, timing exact)."""
    P = rm.load_template("punch")
    words = [Word(*w) for w in fc._seq(0.2, 0.3, "one two three")] + [Word(2.0, 2.4, "four")]
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
                        # the spoken word grows, easing in and out over a tenth of a second
                        "sc": 1 + 0.12 * _ramp((T - w.start) / 0.1) * _ramp((w.active_end + 0.1 - T) / 0.1),
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


def test_adversarial_backgrounds_are_deterministic_and_cover_the_cases():
    if not _numpy():
        pytest.skip("numpy not installed")
    for kind in (*fc.SYNTH, "blocks", "#FFE600"):
        a, b = fc.background(kind, 7, 20, 30, 4, 6), fc.background(kind, 7, 20, 30, 4, 6)
        assert a.shape == (20, 30, 3) and (a == b).all()
    assert (fc.background("flash", 0, 2, 2) == 255).all() and (fc.background("flash", 2, 2, 2) == 0).all()
    assert len({tuple(p) for p in fc.background("noise", 1, 16, 16).reshape(-1, 3)}) > 200


# ------------------------------------------------------------------ every frame of every template


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
