import json
import subprocess

from clipwatcher.ingest import category

REAL = category.stream_category  # captured before the conftest stub replaces it


def _run(out: dict, calls: list):
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    return run


def test_category_from_streamlink_metadata_is_cached(monkeypatch):
    monkeypatch.setattr(category.config, "streamlink_bin", lambda: "streamlink")
    category._CACHE.clear()
    calls: list = []
    t = [1000.0]
    meta = {"metadata": {"category": "Grand Theft Auto VI", "title": "GTA 6 day one"}}
    got = REAL("twitch", "kaicenat", run=_run(meta, calls), now=lambda: t[0])
    assert got == "Grand Theft Auto VI" and calls[0][-1] == "https://www.twitch.tv/kaicenat"
    t[0] += 60
    assert REAL("twitch", "kaicenat", run=_run({}, calls), now=lambda: t[0]) == got
    assert len(calls) == 1
    err = REAL("kick", "x", run=_run({"error": "No playable streams"}, calls), now=lambda: t[0])
    assert err is None and calls[-1][-1] == "https://kick.com/x"


def test_allowed_only_the_channel_games():
    games = ["Grand Theft Auto VI"]
    assert category.allowed("grand theft auto vi", games)
    assert not category.allowed("Just Chatting", games)
    assert not category.allowed("", games)
    assert category.allowed(None, games)  # lookup failed: the judge decides
    assert category.allowed("Just Chatting", [])
