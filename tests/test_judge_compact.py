import json
import subprocess
from types import SimpleNamespace

import pytest

from clipwatcher import config, fastlane
from clipwatcher.judge import compact, frames
from clipwatcher.models import ChatMessage, Word


def test_transcript_compact_groups_phrases_and_drops_bloat():
    whisper = {
        "language": "it",
        "segments": [
            {"start": 0.0, "end": 2.0, "avg_logprob": -0.2, "words": [
                {"start": 0.0, "end": 0.4, "word": " ma", "probability": 0.91},
                {"start": 0.45, "end": 0.9, "word": " cosa", "probability": 0.88},
                {"start": 0.95, "end": 1.4, "word": " fai?", "probability": 0.97},
                {"start": 3.0, "end": 3.5, "word": " NO", "probability": 0.99},
            ]},
        ],
    }  # fmt: skip
    tr = compact.serialize_transcript_compact(whisper)
    assert tr == [[0.0, 1.4, "ma cosa fai?"], [3.0, 3.5, "NO"]]
    assert compact.serialize_transcript_compact([Word(0, 1, "hi"), Word(5, 6, "there")]) == [
        [0.0, 1.0, "hi"],
        [5.0, 6.0, "there"],
    ]
    assert compact.serialize_transcript_compact([]) == []


def test_chat_compact_counts_without_usernames():
    lines = ["a: KEKW", "b: kekw", "c: NO WAY", "a: KEKW", "d: " + "x" * 80]
    ch = compact.compact_chat(lines)
    assert ch["n"] == 5 and ch["u"] == 4
    assert ch["top"][0] == ["KEKW", 3] and len(ch["top"][-1][0]) <= 40
    msgs = [ChatMessage(1, "u1", "LUL"), ChatMessage(2, "u2", "LUL")]
    assert compact.compact_chat(msgs) == {"n": 2, "u": 2, "top": [["LUL", 2]]}


def test_payload_is_minified_and_deterministic():
    kw = dict(streamer="Zackrawrr", platform="twitch", game="GTA VI", language="en", dur=30, peak_s=17,
              transcript=[Word(0, 1, "bro")], chat=["a: LUL"])  # fmt: skip
    a, b = (
        compact.payload_json(compact.build_payload(**kw)),
        compact.payload_json(compact.build_payload(**kw)),
    )
    assert a == b and " " not in a.replace("GTA VI", "")
    d = json.loads(a)
    assert d["meta"] == {
        "s": "zackrawrr",
        "p": "twitch",
        "g": "GTA VI",
        "l": "en",
        "cl": "en",
        "dur": 30.0,
        "pk": 17.0,
    }


def test_copy_language_falls_back_for_other_languages():
    assert compact.copy_language("it") == "it" and compact.copy_language("en") == "en"
    assert compact.copy_language("es") == config.global_config()["judge"]["default_copy_language"]


def test_request_system_prompt_is_static_and_cached(tmp_path):
    img = tmp_path / "f.jpg"
    img.write_bytes(b"\xff\xd8\xff")
    p1 = compact.build_payload(streamer="a", platform="kick", game="g", language="it", dur=20, peak_s=5,
                               transcript=[], chat=[])  # fmt: skip
    p2 = compact.build_payload(streamer="b", platform="twitch", game="h", language="en", dur=40, peak_s=9,
                               transcript=[Word(0, 1, "x")], chat=["u: y"])  # fmt: skip
    r1 = compact.build_request("claude-haiku-5-5", [(0.0, img)], p1)
    r2 = compact.build_request("claude-sonnet-5-5", [(1.5, img)], p2)
    assert r1["system"] == r2["system"]  # identical prefix for every clip -> cache hits
    assert r1["system"][0]["cache_control"] == {"type": "ephemeral"}
    sp = r1["system"][0]["text"]
    for dynamic in ("{streamer", "{login", "{clip", "{date", "{title"):  # no format placeholders
        assert dynamic not in sp
    assert len(sp) > 512 * 4  # well above the 512-token cache minimum
    content = r1["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "[t=0.0s]"} and content[1]["type"] == "image"
    assert content[-1]["type"] == "text" and json.loads(content[-1]["text"])["meta"]["s"] == "a"
    assert r1["thinking"] == {"type": "disabled"} and r2["thinking"] == {"type": "between_tools"}
    assert r1["output_config"]["effort"] == "low"
    assert r1["output_config"]["format"]["schema"] is compact.OUTPUT_SCHEMA


def test_normalize_output_caps_credit_and_flags():
    raw = {"ok": True, "sc": 14, "t0": 5, "t1": 3, "hk": "H" * 50, "tt": "T" * 90,
           "ds": "teaser " * 40 + "\nSubscribe for daily best moments 🔔\n#X #GTA6 #Clip #Shorts",
           "fl": {"sp": True, "mu": False, "gb": False}, "why": "w" * 200}  # fmt: skip
    out = compact.normalize_output(raw, login="Kenetgr", platform="kick", dur=28)
    assert out["sc"] == 10 and (out["t0"], out["t1"]) == (0.0, 28.0)
    assert len(out["hk"]) <= 35 and len(out["tt"]) <= 60 and len(out["why"]) <= 80
    assert len(out["ds"]) <= 220 and "kick.com/kenetgr" in out["ds"] and out["ds"].endswith("#Shorts")
    assert out["ok"] is False  # spoiler flag overrides the model's ok
    with pytest.raises(ValueError):
        compact.normalize_output({"ok": True}, login="a", platform="twitch", dur=10)


def test_frame_times_even_plus_peak():
    t = frames.frame_times(30, n=8, peak_rel=15)
    assert len(t) == 8 and t == sorted(t) and 15.5 in t and 17.0 in t
    assert frames.frame_times(30, n=8) == [round(30 * (i + 0.5) / 8, 1) for i in range(8)]
    assert frames.vision_tokens(480, 270) == 173


@pytest.mark.ffmpeg
def test_keyframes_lowres_size_and_no_exif(tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run(
        [config.ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=1920x1080:r=30",
         "-t", "6", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )  # fmt: skip
    from PIL import Image

    out = frames.keyframes_lowres(src, 1.0, 4.0, n=4, peak_rel=2.0, out_dir=tmp_path / "f")
    assert len(out) >= 4
    for t, p in out:
        with Image.open(p) as im:
            assert im.size == (480, 270) and im.format == "JPEG" and not im.getexif()
        assert 0 <= t <= 4.0


def test_fastlane_trim_words():
    w = [Word(1, 2, "a"), Word(5, 6, "b"), Word(20, 21, "c")]
    assert [(x.start, x.text) for x in fastlane.trim_words(w, 4, 10)] == [(1, "b")]


def test_judge_clip_uses_screen_model_for_screen_task():
    from clipwatcher.judge import claude
    from clipwatcher.models import Candidate

    seen = {}

    def create(**kw):
        seen.update(kw)
        out = {"ok": False, "sc": 3, "t0": 0, "t1": 10, "hk": "MEH", "tt": "meh", "ds": "x",
               "fl": {"sp": False, "mu": False, "gb": False}, "why": "dead air"}  # fmt: skip
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))]
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    r = claude.judge_clip(Candidate("a", 15, 0, 30, 5, 5), [], [], [], task="screen", client=client)
    assert seen["model"] == config.global_config()["judge"]["screen_model"]
    assert r.source == "claude" and not r.ok and r.reject_reason == "dead air"
