import json
from types import SimpleNamespace

from clipwatcher.judge import claude
from clipwatcher.models import Candidate, Word


def cand():
    return Candidate("gioco", 30, 10, 50, 6.0, 7.5)


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
    "funny_score": 8.5, "hype_score": 7, "visual_context_summary": "x", "trim_start_s": -3,
    "trim_end_s": 99, "hook_text": "NON CI CREDO", "title_options": ["A" * 80, "B"],
    "setup_line": "He loses.", "reject_reason": None,
}  # fmt: skip
WORDS = [Word(0, 1, "incredibile"), Word(1, 2, "no")]


def test_judge_ok_validates_and_clamps():
    c = FakeClient(resp(GOOD))
    r = claude.judge_candidate(cand(), WORDS, ["KEKW"], [], client=c)
    assert r.source == "claude" and r.funny_score == 8.5
    assert r.trim_start_s == 0 and r.trim_end_s == 40
    assert all(len(t) <= 60 for t in r.title_options)
    assert c.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert c.kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_refusal_and_max_tokens_fall_back():
    for stop in ("refusal", "max_tokens"):
        r = claude.judge_candidate(cand(), WORDS, [], [], client=FakeClient(resp(GOOD, stop)))
        assert r.source == "heuristic" and r.funny_score == 7.5


def test_exception_and_bad_hook_fall_back():
    r = claude.judge_candidate(cand(), WORDS, [], [], client=FakeClient(exc=RuntimeError("boom")))
    assert r.source == "heuristic"
    bad = dict(GOOD, hook_text="ONE")
    r = claude.judge_candidate(cand(), [], [], [], client=FakeClient(resp(bad)))
    assert r.source == "heuristic" and r.hook_text == "WAIT FOR IT"


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
