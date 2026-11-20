"""A Short never shows the wrong thing as the webcam (round two of the 20 Nov check, 91 labelled channels).

layout.resolve says how sure it is; what the picture cannot decide comes out as source "unsure": the whole
16:9 frame over its blurred copy, no cam panel, no full-screen reframe. Synthetic scenes and maps only.
"""

import pytest

from clipwatcher import layout

from .test_webcam_rect import H, W, cutout_seen, near, parse, resolve_on, scene

np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402


def evidence(**over) -> dict:
    """What layout._cam_evidence gives for a webcam nothing speaks against."""
    e = {
        "rect": [0, 100, 400, 225], "sides": {"l": 1.0, "r": 0.9, "t": 0.92, "b": 0.88}, "banner": [],
        "chords": {"b": (0.66, 0.2, 0.05)}, "strips": {"b": 1.1}, "sure": 0.94, "live": 0.08, "seen": 1.0,
        "cams": [{"rect": [0, 100, 400, 225], "seen": 1.0}],
    }  # fmt: skip
    return {**e, **over}


def full_camera(tmp_path, n: int, seed: int) -> list:
    """Frames of a camera that is the whole picture: a room unlike the webcam's (other colours)."""
    from PIL import ImageOps

    frames, _ = scene(tmp_path, [(0, 0, W, H)], n=n, game=False, seed=seed)
    for p in frames:
        ImageOps.invert(Image.open(p).convert("RGB")).save(p)
    return frames


def whole_frame(lay) -> bool:
    k = lay.as_render_kwargs()
    return lay.source == "unsure" and not k["has_webcam"] and not k["reframe"] and k["cam"] is None


# --------------------------------------------------------------------------- what speaks against a cam
def test_a_webcam_nothing_speaks_against_is_sure():
    assert layout.doubts(evidence()) == []


def test_a_faint_side_is_a_doubt_when_the_rectangle_has_little_else():
    """sodapoppin / xaryu: left and bottom were the frame's, the top a faint line inside the cam, and the
    panel showed the cam's lower half and the game's bar. k4sen: a minimap as lively as the game."""
    faint = {"l": 1.0, "r": 0.9, "t": 0.43, "b": 1.0}
    assert layout.doubts(evidence(sides=faint, sure=0.23))  # three sure sides only
    assert layout.doubts(evidence(sides=faint, live=1.4))  # as alive as what is around it
    # alanzoka / reet: four sure sides, a calm room against a busy game: one faint side is the dark game
    assert layout.doubts(evidence(sides=faint)) == []
    # arrowcs: the side was put where a sponsor banner begins (a dark shirt against a dark banner)
    assert layout.doubts(evidence(sides=faint, live=1.4, banner=["t"])) == []


def test_three_sure_sides_and_a_face_only_when_enlarged_is_a_doubt():
    """787prada: an empty inventory slot, three sure sides, and the 'face' only in the enlarged look.
    Either alone is common among real cams (clix, rubius: three sides; kaicenat: only enlarged)."""
    assert layout.doubts(evidence(sure=0.36, full=0.0))
    assert layout.doubts(evidence(sure=0.36, full=0.5)) == []
    assert layout.doubts(evidence(full=0.0)) == []


def test_a_face_seen_on_and_off_is_a_doubt():
    assert layout.doubts(evidence(seen=0.67))


def test_a_strip_far_livelier_than_the_rest_behind_a_line_is_a_doubt():
    """infume: a keyboard camera attached under the face cam, no border between them that the sides
    show. hasanabi's hands on the desk look the same in numbers, so both are left to the operator."""
    assert layout.doubts(evidence(strips={"b": 5.5}))
    assert layout.doubts(evidence(strips={"b": 5.5}, chords={"b": (0.3, 0.2, 0.05)})) == []  # no line
    assert layout.doubts(evidence(strips={"b": 5.5}, chords={"b": (0.66, 0.6, 0.05)})) == []  # past the face


def test_rectangles_of_different_sizes_need_someone_to_say_which_is_the_streamers():
    """hasanabi: a tall video window with a face beside the cam; warn, pikabooirl: the stream being
    watched. The judge's point (cam_hint) says which; a face there far more often does too."""
    two = [{"rect": [0, 100, 400, 225], "seen": 1.0}, {"rect": [700, 80, 700, 420], "seen": 1.0}]
    assert layout.doubts(evidence(cams=two))
    assert layout.doubts(evidence(cams=two), hinted=True) == []
    often = [{"rect": [0, 100, 400, 225], "seen": 1.0}, {"rect": [700, 80, 700, 420], "seen": 0.5}]
    assert layout.doubts(evidence(cams=often)) == []
    duo = [{"rect": [0, 100, 400, 225], "seen": 1.0}, {"rect": [1500, 100, 410, 230], "seen": 1.0}]
    assert layout.doubts(evidence(cams=duo)) == []  # cams of one size: a duo, a grid


# --------------------------------------------------------------------------- resolve: never a guess
def test_one_face_with_no_rectangle_in_a_moving_picture_is_the_whole_frame(tmp_path, monkeypatch):
    """steel: a camera filming a phone, with a dark picture-in-picture cam nobody could read: the Short
    followed the faces of the videos on the phone. ohtofu / sodapoppin when their cam was missed. A
    handheld camera looks the same, so it gets the whole frame too."""
    frames, _ = scene(tmp_path, [])
    lay = resolve_on(monkeypatch, frames, lambda p: [(430, 90, 110, 120)], peak_s=5.0)
    assert whole_frame(lay) and lay.sure < 0.5 and lay.doubts
    # no face at all: no webcam seen, and nothing to follow full screen either
    lay = resolve_on(monkeypatch, frames, lambda p: [], peak_s=5.0)
    assert lay.source == "no-face" and not lay.as_render_kwargs()["reframe"]


def test_a_cam_the_streamer_switches_away_from_is_the_whole_frame(tmp_path, monkeypatch):
    """arky: the cam overlay is there at the moment and a second later the camera is the whole picture:
    one layout for the clip would show the room's ceiling in the cam panel from there on."""
    cam = (0, 120, 250, 140)
    with_cam, (face,) = scene(tmp_path, [cam], n=10, seed=1)
    without = full_camera(tmp_path, 14, seed=2)  # most of the stretch is the other layout
    assert layout._gone(with_cam, cam) is None
    assert "missing" in layout._gone(with_cam + without, cam, with_cam)
    lay = resolve_on(monkeypatch, with_cam + without, lambda p: [face] if "s1_" in p.name else [], peak_s=5.0)
    assert whole_frame(lay) and "rect" in lay.evidence  # the operator sees which rectangle it was
    lay = resolve_on(monkeypatch, with_cam, lambda p: [face], peak_s=5.0)
    assert lay.has_webcam and lay.sure > 0.5 and not lay.doubts


def test_a_full_camera_after_a_webcam_scene_is_the_whole_frame(tmp_path, monkeypatch):
    """The other way round: at the moment the camera is the picture, earlier in the stretch it was a
    webcam over the game. Following the face full screen would cut the game out of that part."""
    cam = (0, 120, 250, 140)
    game, (face,) = scene(tmp_path, [cam], n=10, seed=1)
    room = full_camera(tmp_path, 10, seed=2)
    people = [(200, 150, 60, 66), (460, 140, 62, 70), (700, 160, 58, 64)]

    def detector(p):
        return [face] if "s1_" in p.name else people

    lay = resolve_on(monkeypatch, game + room, detector, peak_s=15.0)
    assert whole_frame(lay)
    lay = resolve_on(monkeypatch, room, detector, peak_s=5.0)
    assert lay.source == "fullcam" and lay.as_render_kwargs()["reframe"]


def test_the_judges_point_is_not_a_rectangle(tmp_path, monkeypatch):
    """The judge says where a webcam is, not where it ends: with a face found there the rectangle is read
    from the picture around it (myth, kaicenat: small dark cams the full pass missed); with none a box
    around the point is a guess, and a guess is the whole frame."""
    cam = (700, 40, 240, 136)
    frames, (face,) = scene(tmp_path, [cam])
    hint = ((cam[0] + cam[2] / 2) / W, (cam[1] + cam[3] / 2) / H)
    x0 = int(min(max(0, hint[0] * W - 0.15 * W), W - int(0.3 * W)))  # calibrate.face_near's crop, enlarged 2x

    def only_near(p):
        if not p.name.startswith("near_"):
            return []
        return [((face[0] - x0) * 2.0, face[1] * 2.0, face[2] * 2.0, face[3] * 2.0)]

    lay = resolve_on(monkeypatch, frames, only_near, cam_hint=hint)
    assert lay.has_webcam and lay.source == "auto-new" and near(parse(lay.cam), cam)
    assert whole_frame(resolve_on(monkeypatch, frames, lambda p: [], cam_hint=hint))
    assert resolve_on(monkeypatch, frames, lambda p: []).source == "no-face"


def test_a_cut_out_in_the_middle_of_the_bottom_edge_and_two_people_who_are_not_cut_outs(
    tmp_path, monkeypatch
):
    """ohtofu: a green-screen streamer at the bottom centre of a game with a HUD was a 'full-screen
    camera'. fwaz / akanemsko: two people in front of one camera were split, one of them as the cam."""
    frames, _ = scene(tmp_path, [])
    person = (430, 330, 100, 110)
    for i, p in enumerate(frames):
        im = Image.open(p)
        d = ImageDraw.Draw(im)
        for x0, y0 in ((0, 0), (720, 0), (0, 470)):  # a HUD: panels of lettering that never change
            d.rectangle([x0, y0, x0 + 239, y0 + 69], fill=(20, 22, 30))
            for x in range(x0 + 4, x0 + 236, 6):
                d.line([x, y0 + 6, x, y0 + 62], fill=(235, 235, 235), width=2)
        # the face itself changes, the game moves right up to it
        d.ellipse([person[0] + i % 5, person[1], person[0] + person[2] + i % 5, person[1] + person[3]],
                  fill=(200 - 9 * i, 170, 150))  # fmt: skip
        im.save(p)
    lay = resolve_on(monkeypatch, frames, cutout_seen(person), peak_s=5.0)
    assert lay.has_webcam and lay.source == "auto-new"
    x, y, w, h = parse(lay.cam)
    assert x <= person[0] and x + w >= person[0] + person[2] and y <= person[1] and y + h >= 440
    other = (150, 320, 96, 104)
    lay = resolve_on(monkeypatch, frames, lambda p: [person, other], peak_s=5.0)
    assert not lay.has_webcam


def test_a_cut_out_webcam_is_there_through_the_whole_stretch(tmp_path, monkeypatch):
    """leodsumo: a spectator at the ring of a sumo broadcast sat low at the side of a moving picture for
    one shot and became the 'cut-out webcam'. A streamer's cut-out stays where it is."""
    person = (790, 300, 120, 130)
    shot, _ = scene(tmp_path, [], n=10, seed=1)
    after, _ = scene(tmp_path, [], n=10, seed=2)
    for i, p in enumerate(shot):
        im = Image.open(p)
        ImageDraw.Draw(im).ellipse(
            [person[0] + i % 5, person[1], person[0] + person[2] + i % 5, person[1] + person[3]],
            fill=(200 - 9 * i, 170, 150),
        )
        im.save(p)
    sees = cutout_seen(person)

    def detector(p):  # the six enlarged looks are spread over the stretch: three in the shot, three after
        if p.name.startswith("zoom_"):
            return sees(p) if int(p.stem[-2:]) < 3 else []
        return [person] if "s1_" in p.name else []

    lay = resolve_on(monkeypatch, shot + after, detector, peak_s=5.0)
    assert whole_frame(lay) and "3 of 6" in lay.doubts[0]


def test_the_operators_full_camera_scene_decides_one_face_in_a_moving_picture(tmp_path, monkeypatch):
    """D12: an IRL clip is a full-screen window that follows the face. One face in a moving picture is a
    handheld camera or a webcam the layout failed to read (27 moments of the older sets were held back
    for that); a confirmed `has_webcam: false` scene in the streamer's layouts says which. The picture
    still wins when it shows an overlay, and the streamer-level flag is not such a scene."""
    frames, _ = scene(tmp_path, [])
    walker = (430, 90, 110, 120)
    assert whole_frame(resolve_on(monkeypatch, frames, lambda p: [walker], peak_s=5.0))
    confirmed = {"layouts": [{"name": "irl", "has_webcam": False}]}
    lay = resolve_on(monkeypatch, frames, lambda p: [walker], confirmed, peak_s=5.0)
    assert lay.source == "fullcam" and lay.sure > 0.5 and lay.as_render_kwargs()["reframe"]
    lay = resolve_on(monkeypatch, frames, lambda p: [walker], {"has_webcam": False}, peak_s=5.0)
    assert lay.source == "fixed" and not lay.as_render_kwargs()["reframe"]
    cam = (0, 120, 250, 140)
    with_cam, (face,) = scene(tmp_path, [cam], seed=5)
    lay = resolve_on(monkeypatch, with_cam, lambda p: [face], confirmed, peak_s=5.0)
    assert lay.has_webcam and near(parse(lay.cam), cam)


def test_a_full_camera_is_not_sure_beside_a_rectangle_that_could_be_a_webcam(tmp_path, monkeypatch):
    """alfie: a webcam over a still game; the rectangle was there but 'part of a room', and a still
    picture with a face was a full-screen camera. Not even a confirmed full-camera scene makes it one."""
    cam = (600, 40, 300, 170)
    head = (cam[0] + 200, cam[1] + 110, 50, 62)  # low in the cam: its sides are clear on the dark screen
    frames, _ = scene(tmp_path, [cam], game=False, heads={0: head})
    still_face = (cam[0] + 20, cam[1] + 40, 60, 66)  # where nothing in the cam moves: no webcam for find_cams
    lay = resolve_on(monkeypatch, frames, lambda p: [still_face], peak_s=5.0)
    assert whole_frame(lay) and "could be a webcam" in lay.doubts[0]
    confirmed = {"layouts": [{"name": "irl", "has_webcam": False}]}
    assert whole_frame(resolve_on(monkeypatch, frames, lambda p: [still_face], confirmed, peak_s=5.0))
