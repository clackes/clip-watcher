"""D16: keywords and detector settings per kind of stream (category) and per streamer, learned from chat."""

import asyncio
import random

from clipwatcher import config, kickvod
from clipwatcher.chat import kick, twitch_replay
from clipwatcher.detect.detector import detect, settings
from clipwatcher.detect.live import LiveDetector
from clipwatcher.models import ChatMessage
from clipwatcher.session import StreamSession

GLOBAL = {
    "detector": {"keywords": {"KEKW": 2, "W": 1.5}, "keep_q": 0.99},
    "categories": {
        "grand-theft-auto-vi": {
            "match": ["grand theft auto vi", "gta 6"],
            "keywords": {"wasted": 2.5},
            "detector": {"keep_q": 0.985},
        },
        "chatting": {"match": ["just chatting"]},
    },
}
LEARNED = {
    "categories": {
        # the hand-set weight of wasted wins; kekw is KEKW, raised
        "grand-theft-auto-vi": {"keywords": {"busted": 2, "wasted": 1, "kekw": 2.5}},
        "fortnite": {"keywords": {"cracked": 2}},  # a kind that only the learner knows: matched by its name
    },
    "streamers": {"zack": {"keywords": {"asmonl": 2, "W": 1}}},
}


def _patch(monkeypatch, streamers=None):
    monkeypatch.setattr(config, "global_config", lambda: GLOBAL)
    monkeypatch.setattr(config, "learned", lambda: LEARNED)
    monkeypatch.setattr(config, "streamers", lambda: streamers or {})


def test_a_stream_gets_the_keywords_of_its_kind_and_of_its_streamer(monkeypatch):
    _patch(monkeypatch, {"zack": {"keywords": {"bald": 3}}, "rp": {"game": "GTA 6 roleplay"}})
    assert config.category_profile("Grand Theft Auto VI")[0] == "grand-theft-auto-vi"  # Twitch's name
    assert config.category_profile("Grand Theft Auto VI (GTA)")[0] == "grand-theft-auto-vi"  # Kick's
    assert config.category_profile("Fortnite")[0] == "fortnite" and config.category_profile("Chess") == (
        None,
        {},
    )
    assert config.keywords_for("someone") == {"KEKW": 2, "W": 1.5}
    gta = config.keywords_for("someone", "Grand Theft Auto VI")
    assert gta == {"KEKW": 2.5, "W": 1.5, "busted": 2, "wasted": 2.5}
    zack = config.keywords_for(
        "Zack", "Just Chatting"
    )  # no words of GTA; the learned and hand-set ones of zack
    assert zack == {"KEKW": 2, "W": 1.5, "asmonl": 2, "bald": 3}  # learned W 1 does not lower the set 1.5
    assert "busted" in config.keywords_for("rp")  # no category given: the game set in streamers.yaml
    assert (
        settings("someone", "Grand Theft Auto VI")["keep_q"] == 0.985
        and settings("someone")["keep_q"] == 0.99
    )


def _chat(rng, n_s, burst_at, burst_text):
    msgs = [
        ChatMessage((1_790_000_000 + t) * 1000, f"u{rng.randrange(60)}", f"talking about {t}")
        for t in range(n_s)
    ]
    msgs += [
        ChatMessage(int((1_790_000_000 + burst_at + rng.uniform(0, 6)) * 1000), f"fan{i}", burst_text)
        for i in range(12)
    ]
    return sorted(msgs, key=lambda m: m.ts_ms)


def test_a_category_keyword_counts_only_in_that_kind_of_stream(monkeypatch):
    _patch(monkeypatch)
    msgs = _chat(random.Random(1), 900, 700, "WASTED")

    def best(category):
        got = detect(msgs, streamer="someone", top=1, category=category)
        return got[0].signals["zE"] if got else 0.0

    # Twelve chatters typing the same unknown word look like copy-paste the streamer asked for, and that is
    # muted. Where the word is a known reaction of that kind of stream, the same burst is a moment.
    assert best("Grand Theft Auto VI") > 5 and best(None) == 0.0

    det = LiveDetector("someone", category="Just Chatting")
    assert "wasted" not in det._wmap
    det.set_category("Grand Theft Auto VI (GTA)")  # the streamer opened the game
    assert det._wmap["wasted"] == 2.5 and det.category == "Grand Theft Auto VI (GTA)"


def test_the_platforms_say_what_is_being_streamed():
    def twitch(payload):
        stream = {"id": "1", "createdAt": "2026-11-19T18:00:00Z", "game": {"name": "Grand Theft Auto VI"}}
        return {"data": {"user": {"id": "9", "stream": stream}}}

    assert twitch_replay.live_session("x", post=twitch).category == "Grand Theft Auto VI"

    def kick_fetch(url, headers):
        ls = {"id": 5, "is_live": True, "start_time": "2026-11-19 20:11:45",
              "categories": [{"name": "Grand Theft Auto VI (GTA)"}]}  # fmt: skip
        return {"id": 7, "slug": "n3on", "chatroom": {"id": 8}, "livestream": ls}

    assert kick.live_session("n3on", fetch=kick_fetch).category == "Grand Theft Auto VI (GTA)"

    video = {"data": {"video": {"game": {"name": "Grand Theft Auto VI"}}}}
    assert twitch_replay.vod_category("42", post=lambda p: video) == "Grand Theft Auto VI"
    assert twitch_replay.vod_category("42", post=lambda p: {"data": {"video": None}}) is None
    assert (
        twitch_replay.vod_category("42") is None
    )  # offline (tests never reach Twitch): no category, no error
    row = {"id": 5, "video": {"uuid": "ab"}, "start_time": "2026-11-19 20:11:45", "duration": 60000}
    rows = [
        {**row, "channel_id": 7, "source": "https://x/m.m3u8", "categories": [{"name": "Slots & Casino"}]}
    ]
    assert kickvod.fetch_vod_meta("n3on", "ab", get=lambda url: rows)["category"] == "Slots & Casino"


def test_session_follows_the_streamer_into_another_game(tmp_path):
    class Det:
        def __init__(self, login):
            self.category, self.fed = None, []

        def set_category(self, category):
            self.category = category

        def prime(self, history):
            return 0

        def feed(self, msg):
            self.fed.append(msg)

    looks = [
        twitch_replay.Session("1", 1_000_000, None, category="Just Chatting"),
        twitch_replay.Session("1", 1_000_000, None, category="Grand Theft Auto VI"),
    ]
    s = StreamSession("zack", root=tmp_path, make_detector=Det, lookup=lambda login: looks.pop(0), fetch=None)

    async def go():
        await s.refresh()
        first = (s.detector, s.detector.category)
        await s.refresh()  # the same broadcast, now in GTA
        return first

    det, was = asyncio.run(go())
    assert was == "Just Chatting" and s.detector is det and det.category == "Grand Theft Auto VI"
    assert s.category == "Grand Theft Auto VI"
    s.close()
