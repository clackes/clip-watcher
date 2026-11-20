"""The judge's ledger: every call with its tokens, its cost and its answer."""

import json
from types import SimpleNamespace

from clipwatcher import config
from clipwatcher.judge import claude, ledger
from clipwatcher.models import Candidate

BIG = {"input": 2.0, "output": 10.0, "cache_read": 0.1, "cache_write": 2.5}
PRICES = {"judge": {"judge_model": "big", "prices": {"big": BIG}}}
USAGE = {"input_tokens": 2_000, "output_tokens": 500, "cache_read_input_tokens": 4_000}


def test_a_call_is_priced_by_kind_of_token(monkeypatch):
    monkeypatch.setattr(config, "global_config", lambda: PRICES)
    ledger.record(task="judge", model="big", usage=USAGE, streamer="zack", start_s=12.5, ok=True, score=7)
    ledger.record(
        task="judge", model="unlisted", usage=USAGE
    )  # no price for this model: tokens kept, no cost
    ledger.record(task="judge", model="big", error="timeout")
    a, b, c = ledger.read()
    assert a["usd_parts"] == {"input": 0.004, "output": 0.005, "cache_read": 0.0004, "cache_write": 0.0}
    assert a["usd"] == 0.0094 and a["start_ms"] == 12500 and a["score"] == 7 and a["streamer"] == "zack"
    assert b["usd"] is None and b["input_tokens"] == 2_000
    assert c["error"] == "timeout" and c["usd"] == 0.0 and c["output_tokens"] == 0


def test_the_judge_writes_its_call_and_its_failure(monkeypatch):
    monkeypatch.setattr(config, "global_config", lambda: PRICES)
    flags = {"sp": False, "mu": False, "gb": False}
    said = {
        "ok": True,
        "sc": 8,
        "t0": 0,
        "t1": 30,
        "hk": "h",
        "tt": "Title",
        "ds": "d",
        "fl": flags,
        "why": "w",
    }
    usage = SimpleNamespace(input_tokens=1500, output_tokens=300, cache_read_input_tokens=4000)
    text = SimpleNamespace(type="text", text=json.dumps(said))
    resp = SimpleNamespace(content=[text], stop_reason="end_turn", usage=usage)
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: resp))
    cand = Candidate("zack", 15, 0, 30, 5, 5)
    assert claude.judge_clip(cand, [], [], [], client=client).source == "claude"

    def broken(**k):
        raise TimeoutError("no answer in 60 s")

    failing = SimpleNamespace(messages=SimpleNamespace(create=broken))
    assert claude.judge_clip(cand, [], [], [], client=failing).source == "heuristic"
    ok, failed = ledger.read()
    assert (ok["model"], ok["score"], ok["ok"], ok["title"], ok["input_tokens"]) == (
        "big",
        8,
        True,
        "Title",
        1500,
    )
    assert ok["usd"] == round((1500 * 2 + 300 * 10 + 4000 * 0.1) / 1e6, 8)
    assert failed["error"].startswith("no answer") and failed["model"] == "big" and failed["usd"] == 0.0


def test_no_key_and_no_call_leaves_no_row(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert claude.judge_clip(Candidate("zack", 15, 0, 30, 5, 5), [], [], []).source == "heuristic"
    assert ledger.read() == []
