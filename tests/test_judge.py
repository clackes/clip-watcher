import json
from types import SimpleNamespace

from clipwatcher.judge import claude, compact
from clipwatcher.models import Candidate, Word


def cand():
    return Candidate("gamer", 30, 10, 50, 6.0, 7.5)


def resp(payload, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(payload))])


class FakeClient:
    def __init__(self, r=None, exc=None):
        self.r, self.exc, self.kwargs = r, exc, None
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.kwargs = kw
        if self.exc:
            raise self.exc
        return self.r


GOOD = {
    "ok": True, "sc": 8, "t0": -3, "t1": 99, "hk": "THIS CAN'T BE REAL 💀", "tt": "A" * 80,
    "ds": "This was not supposed to happen...\nSkill or luck? 👇\nLike, comment and subscribe!\n#Gamer #Clip",
    "fl": {"sp": False, "mu": False, "gb": False}, "why": "big reaction",
}  # fmt: skip
WORDS = [Word(0, 1, "unbelievable"), Word(1, 2, "no")]


def test_judge_ok_validates_and_clamps():
    c = FakeClient(resp(GOOD))
    r = claude.judge_candidate(cand(), WORDS, ["KEKW"], [], client=c)
    assert r.source == "claude" and r.funny_score == 8 and r.ok
    assert r.trim_start_s == 0 and r.trim_end_s == 40
    assert all(len(t) <= 60 for t in r.title_options)
    assert "twitch.tv/gamer" in r.description  # credit enforced even when the model forgets it
    assert "subscribe" not in r.description.lower()  # generic engagement bait is dropped
    assert "Skill or luck? 👇" in r.description
    assert c.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert c.kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_refusal_and_max_tokens_fall_back():
    for stop in ("refusal", "max_tokens"):
        r = claude.judge_candidate(cand(), WORDS, [], [], client=FakeClient(resp(GOOD, stop)))
        assert r.source == "heuristic" and r.funny_score == 7.5


def test_exception_and_bad_output_fall_back():
    r = claude.judge_candidate(cand(), WORDS, [], [], client=FakeClient(exc=RuntimeError("boom")))
    assert r.source == "heuristic"
    for bad in (dict(GOOD, hk=""), {k: v for k, v in GOOD.items() if k != "fl"}):
        r = claude.judge_candidate(cand(), [], [], [], client=FakeClient(resp(bad)))
        assert r.source == "heuristic" and r.hook_text in compact._FB_HOOKS
        assert "twitch.tv/gamer" in r.description and "👇" in r.description


def test_flags_reject_even_if_model_says_ok():
    r = claude.judge_candidate(
        cand(), WORDS, [], [], client=FakeClient(resp(dict(GOOD, fl={"sp": True, "mu": False, "gb": False})))
    )
    assert r.source == "claude" and not r.ok and r.flags["sp"] and "sp" in r.reject_reason


def test_music_flag_is_informational():
    """Owner, 18 Nov: music-claim risk is accepted; mu stays in meta but never rejects."""
    r = claude.judge_candidate(
        cand(), WORDS, [], [], client=FakeClient(resp(dict(GOOD, fl={"sp": False, "mu": True, "gb": False})))
    )
    assert r.source == "claude" and r.ok and r.flags["mu"]


def test_screen():
    p = {"funny_score": 6, "hype_score": 4, "works_without_context": True, "reject_reason": None}
    assert claude.screen("t", [], "ti", "g", client=FakeClient(resp(p)))["funny_score"] == 6
    assert claude.screen("t", [], "ti", "g", client=FakeClient(exc=ValueError()))["works_without_context"]


def test_load_wav_returns_float_samples(tmp_path):
    import struct
    import wave

    from clipwatcher.judge.transcribe import load_wav

    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<3h", 0, 16384, -32768))
    arr = load_wav(p)
    assert arr.dtype.name == "float32" and list(arr) == [0.0, 0.5, -1.0]
