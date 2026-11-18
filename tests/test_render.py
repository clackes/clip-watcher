from __future__ import annotations

import json
import re
import subprocess

import pytest

from clipwatcher.config import ffmpeg_bin, ffprobe_bin
from clipwatcher.models import Word
from clipwatcher.render.branding import hook_pill
from clipwatcher.render.captions import build_ass, caption_style, demo_words, esc
from clipwatcher.render.encoders import resolve, video_args
from clipwatcher.render.template import (
    esc_filter_path,
    pick_style,
    render_clean,
    render_short,
)

from .conftest import SRC_SECONDS, second_colour


def probe(p):
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-count_frames", "-show_entries",
         "stream=codec_type,width,height,pix_fmt,nb_read_frames,avg_frame_rate", "-show_entries",
         "format=duration", "-of", "json", str(p)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(r.stdout)


def loudness(p) -> float:
    r = subprocess.run(
        [ffmpeg_bin(), "-nostats", "-i", str(p), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True,
    )  # fmt: skip
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)[-1])


def first_pixel(p, x=100, y=100) -> tuple[int, int, int]:
    vf = f"select=eq(n\\,6),crop=8:8:{x}:{y},format=rgb24"
    r = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-i", str(p), "-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )  # fmt: skip
    return tuple(r.stdout[:3])


def check_short(out, dur, fps=60):
    info = probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["pix_fmt"]) == (1080, 1920, "yuv420p")
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert abs(float(info["format"]["duration"]) - dur) <= 1 / fps + 0.03
    assert int(v["nb_read_frames"]) == round(dur * fps)
    assert abs(loudness(out) - -14) <= 1.0


def test_pure_helpers():
    assert pick_style("abc") == pick_style("abc")
    assert esc_filter_path("/a b/c:d'e") == "/a b/c\\:d\\'e"
    ass = build_ass(demo_words(4), 4, credit="twitch.tv/x")
    assert "PlayResX: 1080" in ass and "\\pos(540,1495)" in ass and "&H00E6FF&" in ass
    assert "\\an9\\pos(1060,407)" in ass
    assert "{" not in esc("a{b}\\c")
    pill = hook_pill("x" * 200)
    assert pill.size[1] == 96 and pill.size[0] <= 1000
    assert resolve("nope") == "cpu_batch" and "libx264" in video_args("cpu_live")


def test_bounce_captions():
    words = demo_words(6, "NO WAY DID YOU SEE THAT UNBELIEVABLE")
    ass = build_ass(words, 6, style="bounce")
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 0")]
    assert len(events) == len(words)  # one event per spoken word
    pops = [e for e in events if "\\t(0,90,\\fscx118" in e]
    assert 0 < len(pops) < len(events)  # page pops in once, on its first word
    assert "UNBELIEVABLE" in pops[-1]  # 12 chars: never shares a page past 14 chars
    assert "&HFFF000&" in ass  # colour rotates to cyan on page 2
    with pytest.raises(ValueError):
        build_ass(words, 6, style="nope")


def test_caption_style_ab_is_stable_and_splits(monkeypatch):
    from clipwatcher import config

    monkeypatch.setattr(config, "global_config", lambda: {"render": {"captions": "ab"}})
    picks = {caption_style(f"x-{i}") for i in range(40)}
    assert picks == {"classic", "pop"} and caption_style("x-1") == caption_style("x-1")
    monkeypatch.setattr(config, "global_config", lambda: {"render": {"captions": "bounce"}})
    assert caption_style("x-1") == "bounce"


@pytest.mark.ffmpeg
@pytest.mark.parametrize(
    "style,webcam,captions",
    [("ambient", True, "classic"), ("gradient", True, "bounce"), ("ambient", False, "bounce")],
)
def test_short(synth_src, tmp_path, style, webcam, captions):
    out = tmp_path / "s.mp4"
    render_short(
        synth_src, 8.0, 3.0, out, clip_id="t", streamer="tester", words=demo_words(3),
        hook="HE DID NOT SEE THAT", style=style, accent="cyan", has_webcam=webcam, profile="cpu_live",
        captions=captions, layout="stack",
    )  # fmt: skip
    check_short(out, 3.0)


@pytest.mark.ffmpeg
@pytest.mark.parametrize("webcam", [True, False])
def test_split_short_with_beats_and_cold_open(synth_src, tmp_path, webcam):
    out = tmp_path / "split.mp4"
    beats = [
        (1.0, "hype", "*SUCCESS*", "anime-wow"),
        (2.5, "fail", "*FAIL*", "fahhh"),
        (3.5, "shock", "", ""),
    ]
    render_short(
        synth_src, 4.0, 4.0, out, clip_id="sp", streamer="tester", words=demo_words(4), hook="unused",
        has_webcam=webcam, profile="cpu_live", captions="word", cold_open=(2.5, 1.5), beats=beats,
        layout="split",
    )  # fmt: skip
    check_short(out, 5.5)


def test_split_helpers():
    from clipwatcher.render.template import fit_crop, zoom_expr

    assert fit_crop((0, 304, 494, 220), 1080 / 960) == (124, 304, 246, 220)  # wide cam -> centre strip
    x, y, w, h = fit_crop((0, 304, 494, 220), 1080 / 960, centre=(10, 400))
    assert x == 0 and w == 246  # clamped inside the cam
    assert zoom_expr(0, 1.15, []) is None
    z = zoom_expr(2.0, 1.15, [5.0])
    assert "lt(it,2.000)" in z and "between(it,5.000,5.450)" in z


def test_word_captions_one_word_and_labels_replace_words():
    words = [Word(0.0, 0.3, "no"), Word(0.35, 0.6, "way"), Word(1.0, 1.3, "bro"), Word(3.0, 3.2, "ok")]
    ass = build_ass(words, 4.0, style="word", y=960, beats=[(0.9, "fail", "*FAIL*"), (2.0, "shock", "")])
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue")]
    assert any("*FAIL*" in e and "&H2A2AFF&" in e for e in events)
    assert not any(e.endswith("BRO") for e in events)  # hidden under the label
    assert any(e.endswith("NO") and "&H00E6FF&" in e for e in events)  # emphasis word in yellow
    assert all("pos(540,960)" in e for e in events)


@pytest.mark.ffmpeg
def test_short_cpu_batch_social(synth_src, tmp_path):
    out = tmp_path / "b.mp4"
    render_short(
        synth_src, 2.0, 3.0, out, clip_id="t2", streamer="tester", words=[], hook=None,
        platform="social", profile="cpu_batch",
    )  # fmt: skip
    check_short(out, 3.0)


@pytest.mark.ffmpeg
def test_clean(synth_src, tmp_path):
    out = tmp_path / "c.mp4"
    render_clean(synth_src, 5.0, 3.0, out, profile="cpu_live")
    info = probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1920, 1080)
    assert abs(loudness(out) - -14) <= 1.0


@pytest.mark.ffmpeg
@pytest.mark.parametrize("start", [7.0, 9.5, 13.0, 16.0])
def test_d9_ts_seek_lands_on_requested_second(synth_src, tmp_path, start):
    """Keyframes are 4 s apart: a naive exact -ss on .ts would land on the wrong second.

    Frame 6 (0.1 s in) is sampled because the synthetic .ts video starts ~1.3 frames after its audio.
    """
    assert start + 2 <= SRC_SECONDS
    out = tmp_path / "d9.mp4"
    render_clean(synth_src, start, 2.0, out, profile="cpu_live")
    got = first_pixel(out)
    k = int(start)
    best = min(
        range(SRC_SECONDS), key=lambda i: sum(abs(a - b) for a, b in zip(second_colour(i), got, strict=False))
    )
    assert best == k, (got, second_colour(k))


def test_parse_rect_accepts_config_strings():
    from clipwatcher.render.template import parse_rect

    assert parse_rect("520:231:0:500") == (0, 500, 520, 231)
    assert parse_rect((1, 2, 3, 4)) == (1, 2, 3, 4)
    assert parse_rect(None) is None


def test_pop_captions_one_event_set_per_word():
    words = demo_words(5, "NO WAY DID YOU SEE THAT")
    ass = build_ass(words, 5, style="pop")
    active = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 2")]
    assert len(active) == len(words)  # each word pops exactly once, when spoken
    assert all("\\t(0,70,\\fscx135" in ln for ln in active)
    colours = {re.search(r"\\c(&H[0-9A-F]+&)", ln).group(1) for ln in active}
    assert colours == {"&H00E6FF&"}  # only the spoken word is coloured
    assert not [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 1,") and "\\c" in ln]
    assert "Style: Pop3,Anton," in ass


def test_cold_open_window(monkeypatch):
    from clipwatcher import config
    from clipwatcher.render import cold_open

    monkeypatch.setattr(config, "global_config", lambda: {"render": {"cold_open_s": 3.5}})
    assert cold_open.window(17.0, 7.0, 23.0) == (7.5, 3.5)  # 2.5 s of build-up before the peak
    assert cold_open.window(8.0, 7.0, 23.0) is None  # already in the first 3 s
    assert cold_open.window(None, 0.0, 30.0) is None
    assert cold_open.window(29.0, 0.0, 30.0) == (26.5, 3.5)
    assert cold_open.window(29.5, 0.0, 30.0) == (27.0, 3.0)  # clipped at the clip end
    # word-safe: start moves to the start of the word in progress, end finishes a short word
    ws = [Word(14.3, 14.7, "OH"), Word(17.8, 18.2, "NO")]
    assert cold_open.window(17.0, 7.0, 23.0, ws) == (7.22, 4.23)
    monkeypatch.setattr(config, "global_config", lambda: {"render": {"cold_open_s": 0}})
    assert cold_open.window(17.0, 7.0, 23.0) is None


@pytest.mark.ffmpeg
@pytest.mark.parametrize("webcam", [True, False])
def test_short_with_cold_open(synth_src, tmp_path, webcam):
    out = tmp_path / "co.mp4"
    render_short(
        synth_src, 4.0, 4.0, out, clip_id="co", streamer="tester", words=demo_words(4),
        hook="WATCH HIS FACE", has_webcam=webcam, profile="cpu_live", captions="pop", cold_open=(2.5, 1.5),
    )  # fmt: skip
    check_short(out, 5.5)


def test_short_graph_cold_open_hides_hook_and_uses_measured_loudnorm(tmp_path):
    from clipwatcher.render.template import build_short_graph

    m = {
        "input_i": "-20.1",
        "input_tp": "-3.0",
        "input_lra": "6.0",
        "input_thresh": "-30.5",
        "target_offset": "0.2",
    }
    g = build_short_graph(
        trim=6.0, dur=20.0, style="gradient", has_webcam=True, has_hook=True, cam=(0, 0, 480, 270),
        game=(0, 0, 1920, 960), gradient_idx=4, logo_idx=1, hook_idx=3, ass=tmp_path / "s.ass",
        fontsdir=tmp_path, hook_s=3.5, cold_open=(9.0, 3.5), measured=m,
    )  # fmt: skip
    assert "enable='between(t,3.500,7.000)'" in g  # hook pill only after the cold open
    assert "volume=6.10dB,alimiter" in g and "loudnorm" not in g


def test_audio_reaction_needs_a_shout_with_an_exclamation():
    from clipwatcher.render.cold_open import audio_reaction

    # talking at -24, a loud stretch of plain talk at -12 (3-6 s), a shout -10 with "NO!" at 14.0-15.5 s
    def level(t):
        return -12.0 if 3 <= t < 6 else (-10.0 if 14.0 <= t < 15.5 else -24.0)

    series = [(round(i * 0.1, 1), level(i * 0.1)) for i in range(300)]
    words = [Word(3.2, 3.6, "okay"), Word(3.7, 4.0, "so"), Word(14.1, 14.5, "NO!"), Word(14.6, 15.0, "bro")]
    r = audio_reaction(series, 0, 30, words)
    assert r is not None and 13.0 <= r <= 14.2  # onset of the shout, not the loud talk at 3 s
    assert audio_reaction(series, 0, 30, [Word(3.2, 3.6, "okay")]) is None  # loud but no exclamation
    flat = [(round(i * 0.1, 1), -20.0) for i in range(300)]
    assert audio_reaction(flat, 0, 30, words) is None


def test_plan_cold_open_only_with_the_judge(monkeypatch, tmp_path):
    from clipwatcher import config
    from clipwatcher.render import cold_open

    monkeypatch.setattr(config, "global_config", lambda: {"render": {"cold_open_s": 2.0}})
    # loud burst at 15.0-16.0 s on a -24 talking bed
    series = [(round(i * 0.1, 1), -10.0 if 15.0 <= i * 0.1 < 16.0 else -24.0) for i in range(300)]
    monkeypatch.setattr(cold_open, "loudness", lambda m, s, d: series)
    heur = {"source": "heuristic", "trim_start_s": 0.0, "trim_end_s": 30.0}
    pl = cold_open.plan(tmp_path / "x.mp4", 0.0, 30.0, 20.0, heur, [])
    assert pl.cold_open is None and pl.t0 == 7.0  # late start only, no guessed cold open
    judged = {"source": "claude", "trim_start_s": 5.0, "trim_end_s": 28.0, "peak_rel_s": 14.0}
    pl = cold_open.plan(tmp_path / "x.mp4", 0.0, 30.0, 20.0, judged, [])
    assert (pl.t0, pl.t1) == (5.0, 28.0)
    assert 15.0 <= pl.reaction <= 15.5  # pk 14.0 moved onto the burst
    assert pl.cold_open is not None and abs(pl.cold_open[0] - (pl.reaction - 2.5 - 5.0)) < 0.01


def test_cold_open_starts_with_the_sentence():
    """Regression (officermesser): the cold open began at "my registration", mid-way through the joke."""
    from clipwatcher.render.cold_open import sentence_start

    text = "let me see your ID. Yeah, right now I'm gonna give you guys my ID and my registration. What"
    times = [20.86, 20.92, 20.92, 20.92, 20.92, 20.92, 20.94, 21.08, 21.26, 21.32, 21.38, 21.56, 21.68,
             21.78, 22.08, 22.24, 22.58, 22.66, 23.64]  # fmt: skip
    ws = [Word(t, t + 0.15, x) for t, x in zip(times, text.split(), strict=True)]
    assert sentence_start(22.59, ws) == 20.84  # "Yeah," right after "ID."
    assert sentence_start(23.5, ws) == 23.5  # pause after a finished sentence: unchanged
    long = [Word(10.0 + i * 0.3, 10.25 + i * 0.3, "word") for i in range(30)]  # one 9 s run-on sentence
    assert sentence_start(17.0, long) == 16.82  # too far back: only the word in progress


def test_hook_pill_drops_emoji():
    """Regression: the pill font has no emoji, "HE ACTUALLY SCORED 💀" showed an empty box."""
    from clipwatcher.render.branding import _strip_emoji

    assert " ".join(_strip_emoji("He didn't see it 💀").split()) == "He didn't see it"
    assert _strip_emoji("WAIT… 😳🔥").strip() == "WAIT…"


def test_pick_sfx_uses_the_judges_sound_aligned_on_its_attack():
    from clipwatcher import sfx
    from clipwatcher.render.template import pick_sfx

    beats = [(1.0, "fail", "*FAIL*", "metal-pipe"), (3.0, "hype", "", ""), (5.0, "shock", "", "nope"),
             (0.5, "shock", "", "among-us-reveal")]  # fmt: skip
    out = pick_sfx(beats)
    assert [(round(t, 2), f.name) for t, f in out] == [(0.82, "metal-pipe.mp3"), (0.0, "among-us-reveal.mp3")]
    assert all(sfx.path(n) for n in sfx.CATALOG)  # every catalogued sound is on disk


def test_sfx_catalog_is_in_the_judge_prompt_and_schema():
    from clipwatcher import sfx
    from clipwatcher.judge import compact

    prompt = compact.system_prompt()
    assert all(f'"{n}" ({k}): {use}' in prompt for n, (k, _, use) in sfx.CATALOG.items())
    item = compact.OUTPUT_SCHEMA["properties"]["fx"]["items"]
    assert item["properties"]["sx"]["enum"] == ["", *sfx.CATALOG]


def test_beat_snaps_to_the_loudness_rise():
    from clipwatcher.render.cold_open import onset

    series = [(round(i * 0.1, 1), -30.0 if i < 52 else -12.0) for i in range(100)]  # crash at 5.2 s
    assert abs(onset(series, 5.8) - 5.2) <= 0.1  # judge said 5.8 from a frame -> moved onto the crash
    flat = [(round(i * 0.1, 1), -20.0) for i in range(100)]
    assert onset(flat, 5.8) == 5.8  # no clear rise: the judge's time stands


def test_jump_cuts_drop_dead_air_but_keep_protected_and_edges():
    from clipwatcher.render import jumpcut

    w = [Word(0.0 + i * 0.3, 0.25 + i * 0.3, "go") for i in range(4)]  # talk 0-1.15
    w += [Word(4.0 + i * 0.3, 4.25 + i * 0.3, "go") for i in range(4)]  # pause 1.15-4.0, talk 4-5.15
    w += [Word(9.0, 9.3, "end"), Word(9.4, 9.7, "now")]  # pause 5.15-9.0
    keep = jumpcut.keep_ranges(w, 11.0, gap=0.8)  # 3.85 s pause cut; 11 s clip: floor = its length
    assert keep == [(0.0, 11.0)]
    keep = jumpcut.keep_ranges(w + [Word(t, t + 0.25, "go") for t in (11.0, 11.3, 11.6)], 30.0, gap=0.8)
    assert keep[:2] == [(0.0, 1.45), (3.85, 5.45)]  # the 2.4 s pause goes; the 3.85 s one before 9 s stays
    assert jumpcut.remap(4.0, keep) == 1.6 and jumpcut.remap(2.0, keep) is None
    # a beat inside the second pause keeps it
    keep2 = jumpcut.keep_ranges(w, 30.0, gap=0.8, protect=[(6.0, 7.0)])
    assert keep2 == [(0.0, 1.45), (3.85, 30.0)]
    talk = [Word(i, i + 0.3, "go") for i in (0, 1, 2, 9, 10, 11)]
    assert jumpcut.keep_ranges(talk, 30.0) == [(0.0, 30.0)]  # a 6.7 s silence is content: never cut
    assert jumpcut.keep_ranges(w[:3], 11.0) == [(0.0, 11.0)]  # almost no speech: visual clip, no cuts


def test_action_labels_replace_words_on_their_stretch():
    words = [Word(0.0, 0.3, "hey"), Word(2.0, 2.3, "inside"), Word(5.0, 5.3, "after")]
    ass = build_ass(words, 6.0, style="word", y=960, actions=[(1.0, 4.0, "*WATCHING*")])
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue")]
    assert any(e.endswith("*WATCHING*") and ",0:00:01.00,0:00:04.00," in e for e in events)
    assert not any(e.endswith("INSIDE") for e in events) and any(e.endswith("AFTER") for e in events)


@pytest.mark.ffmpeg
def test_split_short_with_jump_cuts_and_actions(synth_src, tmp_path):
    out = tmp_path / "jc.mp4"
    w = [Word(i * 0.2, i * 0.2 + 0.15, "go") for i in range(5)] + [
        Word(4.0 + i * 0.2, 4.15 + i * 0.2, "go") for i in range(3)
    ]
    render_short(
        synth_src, 2.0, 5.0, out, clip_id="jc", streamer="tester", words=w, hook=None, profile="cpu_live",
        captions="word", layout="split", actions=[(1.5, 2.5, "*WATCHING*")],
    )  # fmt: skip
    check_short(out, 5.0 - 0.0)  # the protected action stretch keeps the pause: nothing cut here


def test_cues_move_label_and_sound_off_speech():
    from clipwatcher.render.template import cue_time, cues

    words = [Word(1.0, 1.3, "did"), Word(1.35, 1.6, "he"), Word(1.65, 2.0, "rob"), Word(2.6, 3.0, "you")]
    assert cue_time(0.2, words) == 0.2  # nobody talks: stays
    assert cue_time(1.4, words) == 2.05  # mid-line: first pause after it
    assert cue_time(1.4, words + [Word(2.05, 2.55, "and"), Word(2.9, 4.0, "on")]) is None
    beats = [(1.4, "funny", "*ROBBED*", "bruh"), (5.0, "shock", "", "")]
    assert cues(beats, words) == [(2.05, "funny", "*ROBBED*", "bruh")]  # no label/sound: nothing to cue


def test_reframe_path_smooths_and_builds_a_clamped_expression():
    from clipwatcher.render import reframe

    samples = [
        (i * 0.5, x) for i, x in enumerate([None, 0.30, 0.31, 0.95, 0.30, 0.32, 0.70, 0.71, 0.72, 0.72])
    ]
    assert reframe.coverage(samples) == 0.9
    path = reframe.smooth(samples)
    assert path[0][1] == 0.30  # gap backfilled with the first face
    assert max(x for _, x in path) < 0.8  # the 0.95 blip is filtered out
    assert path[-1][1] > 0.6  # the real move to the right is followed
    expr = reframe.x_expr(reframe.keyframes(path), 1920, 606)
    assert expr.startswith("if(lt(t,") and "t-" in expr
    assert reframe.x_expr([(0.0, 0.0)], 1920, 606) == "if(lt(t,0.00),0,0)"  # clamped at the left edge
    steps = [abs(b[1] - a[1]) for a, b in zip(path, path[1:], strict=False)]
    assert max(steps) <= reframe.MAX_SPEED * reframe.SAMPLE_S + 1e-9  # no whip pans


@pytest.mark.ffmpeg
def test_split_short_reframes_a_full_camera_clip(synth_src, tmp_path, monkeypatch):
    from clipwatcher.render import reframe

    monkeypatch.setattr(reframe, "track", lambda *a, **k: [(i * 0.5, 0.3 + 0.05 * i) for i in range(8)])
    out = tmp_path / "rf.mp4"
    render_short(
        synth_src, 2.0, 4.0, out, clip_id="rf", streamer="tester", words=demo_words(4), hook=None,
        has_webcam=False, profile="cpu_live", captions="word", layout="split", reframe=True,
        beats=[(1.0, "shock", "*WAIT*", "")],
    )  # fmt: skip
    check_short(out, 4.0)


def test_split_game_crop_is_full_height_centre():
    """Owner, 18 Nov: the gameplay half showed sky (strip above a bottom-left cam), not the action."""
    from clipwatcher.render.template import split_game_crop

    assert split_game_crop(None, 1920, 1080) == (352, 0, 1214, 1080)
    assert split_game_crop((345, 760, 210, 320), 1920, 1080) == (352, 0, 1214, 1080)
    assert split_game_crop(None, 1280, 720) == (234, 0, 810, 720)


def test_still_edges_cut_no_signal_head_and_frozen_tail():
    """Owner, 18 Nov: a clip opened on 2 s of an Elgato NO SIGNAL card."""
    from clipwatcher.render.still import edges, freezes

    err = (
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 0\n"
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_duration: 2.1\n"
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_end: 2.1\n"
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 9\n"
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_end: 10.5\n"
        "[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 18.2\n"
    )
    spans = freezes(err)
    assert spans == [(0.0, 2.1), (9.0, 10.5), (18.2, None)]
    head, tail = edges(spans, 20.0)
    assert head == 2.1 and round(tail, 2) == 1.8  # the freeze in the middle stays
    assert edges([], 20.0) == (0.0, 0.0)


def test_no_credit_on_screen_by_default():
    from clipwatcher.render.template import screen_credit

    assert screen_credit("xqc") is None
