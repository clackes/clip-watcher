from __future__ import annotations

from pathlib import Path

from clipwatcher import clipslane
from clipwatcher.clipslane import Seen, SourceClip, parse_kick, parse_twitch, select

KICK = {
    "clips": [
        {"id": "clip_A", "title": " lol ", "views": 50, "duration": 30, "privacy": "public",
         "created_at": "2026-11-18T04:57:28.343886Z", "video_url": "https://clips.kick.com/a/playlist.m3u8"},
        {"id": "clip_B", "title": "private", "views": 900, "duration": 30, "privacy": "private",
         "created_at": "2026-11-18T04:57:28Z", "video_url": "https://clips.kick.com/b/playlist.m3u8"},
    ]
}  # fmt: skip
HELIX = {
    "data": [
        {"id": "Slug-1", "title": "W", "view_count": 300, "duration": 28.5,
         "created_at": "2026-11-17T10:00:00Z", "url": "https://clips.twitch.tv/Slug-1"}
    ]
}  # fmt: skip


def test_parse_sources():
    k = parse_kick(KICK, "kenetgr")
    assert [(c.id, c.title, c.views, c.platform) for c in k] == [("clip_A", "lol", 50, "kick")]
    assert k[0].url == "https://kick.com/kenetgr/clips/clip_A" and k[0].media.endswith(".m3u8")
    assert k[0].created_s > 1.7e9
    t = parse_twitch(HELIX, "kaicenat")
    assert (t[0].media, t[0].views, t[0].duration, t[0].platform) == ("Slug-1", 300, 28.5, "twitch")


def _c(id_, views, dur=30.0, plat="kick"):
    return SourceClip(plat, "x", id_, "", views, dur, 0.0, "", "")


def test_select_filters_seen_views_and_length_and_caps():
    s = {"min_views": {"twitch": 100, "kick": 20}, "per_streamer": 2, "min_s": 12, "max_s": 90}
    clips = [_c("a", 500), _c("b", 10), _c("c", 900, dur=180), _c("d", 300), _c("e", 400), _c("f", 50, dur=8),
             _c("t", 90, plat="twitch")]  # fmt: skip
    assert [c.id for c in select(clips, {"e"}, s)] == ["a", "d"]


def test_seen_persists(tmp_path):
    s = Seen(tmp_path / "seen.json")
    s.add("clip_A")
    assert Seen(tmp_path / "seen.json").ids() == {"clip_A"}


def test_run_once_stages_only_judged_ok_and_never_repeats(tmp_path, monkeypatch):
    clips = [_c("a", 500), _c("b", 400)]
    monkeypatch.setattr(
        clipslane,
        "gather",
        lambda names, period, twitch=None: [
            SourceClip(c.platform, "kenetgr", c.id, c.title, c.views, c.duration, 0.0, "", "") for c in clips
        ],
    )
    calls = []

    def fake_process(c, *, work, staging_root):
        calls.append(c.id)
        if c.id == "b":
            return None  # judge said no
        p = staging_root / f"kenetgr-{c.id}"
        p.mkdir(parents=True)
        return p

    monkeypatch.setattr(clipslane, "process", fake_process)
    posted = []
    staged = clipslane.run_once(["kenetgr"], staging_root=tmp_path, work=tmp_path / "w",
                                post=lambda cid, short: posted.append((cid, short.name)))  # fmt: skip
    assert staged == ["kenetgr-a"] and posted == [("kenetgr-a", "short_9x16.mp4")]
    assert calls == ["a", "b"]
    assert clipslane.run_once(["kenetgr"], staging_root=tmp_path, work=tmp_path / "w") == []  # seen
    assert calls == ["a", "b"]


def test_twitch_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)
    assert not clipslane.Twitch().enabled


def test_twitch_top_resolves_user_then_lists_clips(monkeypatch):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "sec")
    urls = []

    def get(url, headers=None):
        urls.append(url)
        assert headers["Client-Id"] == "cid" and headers["Authorization"] == "Bearer tok"
        return {"data": [{"id": "42"}]} if "/users?" in url else HELIX

    tw = clipslane.Twitch(get=get)
    tw._token = "tok"
    out = tw.top("kaicenat", 1.7e9)
    assert [c.id for c in out] == ["Slug-1"] and "broadcaster_id=42" in urls[1]
    tw.top("kaicenat", 1.7e9)
    assert sum("/users?" in u for u in urls) == 1  # broadcaster id cached


def test_process_returns_none_without_media(tmp_path, monkeypatch):
    monkeypatch.setattr(clipslane, "download", lambda c, out: (_ for _ in ()).throw(RuntimeError("404")))
    try:
        clipslane.process(_c("z", 100), work=tmp_path, staging_root=tmp_path)
    except RuntimeError as e:
        assert "404" in str(e)
    assert not list(Path(tmp_path).glob("*.mp4"))
