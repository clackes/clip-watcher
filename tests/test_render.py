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
    beats = [(1.0, "hype", "*SUCCESS*"), (2.5, "fail", "*FAIL*"), (3.5, "shock", "")]
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
