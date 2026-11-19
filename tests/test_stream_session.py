"""D13: one chat log and one primed detector per broadcast (session.StreamSession)."""

import asyncio
import json

from clipwatcher.chat import twitch_replay
from clipwatcher.chat.live_log import LiveLog
from clipwatcher.models import ChatMessage
from clipwatcher.session import StreamSession


# -------------------------------------------------------------------------------- the stream session
class _Det:
    """Records what a session does with its detector."""

    def __init__(self, login):
        self.primed, self.fed, self.spikes = [], [], []

    def prime(self, history):
        self.primed = list(history)
        return len(self.primed)

    def feed(self, msg):
        self.fed.append(msg)
        return None

    def drain(self):
        return []


def _msg(ts_s, text):
    return ChatMessage(int(ts_s * 1000), "u", text)


def _session(tmp_path, now_s, replay, lookups, calls):
    def fetch(vod_id, start_s, end_s):
        calls.append((vod_id, round(start_s), round(end_s)))
        return [m for m in replay if 1000 + start_s <= m.ts_ms / 1000 <= 1000 + end_s]

    def lookup(login):
        return lookups.pop(0) if lookups else None

    return StreamSession(
        "Streamer", root=tmp_path, make_detector=_Det, lookup=lookup, fetch=fetch, clock=lambda: now_s
    )


def test_session_joins_mid_stream_with_the_whole_chat(tmp_path):
    calls: list = []
    replay = [_msg(1000 + t, f"old {t}") for t in range(10, 3600, 60)]  # the stream started at t=1000
    live = twitch_replay.Session("777", 1_000_000, "42")
    s = _session(tmp_path, 4600.0, replay, [live], calls)

    async def go():
        task = asyncio.create_task(s.refresh())
        await asyncio.sleep(0)  # the lookup is under way: chat arriving now is held, not lost
        assert s.feed(_msg(4601, "live 1")) is None and s.detector is None
        await task
        s.feed(_msg(4602, "live 2"))

    asyncio.run(go())
    assert calls[0] == ("42", -30, 3630)  # from the first second of the stream to now
    assert calls[1] == ("42", 3540, 3631)  # ... and the last half minute again, up to the first live message
    assert [m.text for m in s.detector.primed] == [m.text for m in replay]
    assert [m.text for m in s.detector.fed] == ["live 1", "live 2"]  # history is baseline, live is detection
    saved = [
        json.loads(line)["text"] for line in (tmp_path / "streamer" / "777.jsonl").read_text().splitlines()
    ]
    assert saved == [m.text for m in replay] + ["live 1", "live 2"]
    s.close()


def test_session_restart_reads_its_log_and_fetches_only_the_gap(tmp_path):
    calls: list = []
    earlier = LiveLog("streamer", "777", root=tmp_path)
    for t in range(1000, 2000, 50):
        earlier.write(_msg(t, f"saved {t}"))
    earlier.close()
    replay = [_msg(t, f"gap {t}") for t in range(1000, 3000, 100)]
    s = _session(tmp_path, 3000.0, replay, [twitch_replay.Session("777", 1_000_000, "42")], calls)
    asyncio.run(s.refresh())
    # only the 1950 -> 3000 hole (padded 30 s), then a second look at the last half minute before "now"
    assert [c[1:] for c in calls] == [(920, 2030), (1940, 2030)]
    texts = [m.text for m in s.detector.primed]
    assert texts[:20] == [f"saved {t}" for t in range(1000, 2000, 50)]
    assert texts[20:] == [f"gap {t}" for t in range(2000, 3000, 100)]
    s.close()


def test_session_new_broadcast_gets_its_own_log_and_detector(tmp_path):
    first, second = twitch_replay.Session("1", 1_000_000, None), twitch_replay.Session("2", 9_000_000, None)
    s = _session(tmp_path, 9100.0, [], [first, first, second], [])

    async def go():
        await s.refresh()
        s.feed(_msg(1500, "day one"))
        d1 = s.detector
        await s.refresh()  # same broadcast: nothing changes
        assert s.detector is d1
        await s.refresh()
        s.feed(_msg(9200, "day two"))
        return d1

    d1 = asyncio.run(go())
    assert s.stream_id == "2" and s.detector is not d1 and [m.text for m in s.detector.fed] == ["day two"]
    assert (tmp_path / "streamer" / "1.jsonl").read_text().count("day one") == 1
    assert (tmp_path / "streamer" / "2.jsonl").read_text().count("day two") == 1
    s.close()


def test_session_without_twitch_still_detects_and_saves(tmp_path):
    def down(login):
        raise OSError("no network")

    s = StreamSession("streamer", root=tmp_path, make_detector=_Det, lookup=down, fetch=None)
    asyncio.run(s.refresh())
    s.feed(_msg(10, "hello"))
    assert s.stream_id is None and [m.text for m in s.detector.fed] == ["hello"]
    kick = StreamSession("kickguy", root=tmp_path, platform="kick", make_detector=_Det)
    kick.feed(_msg(11, "no refresh ran at all"))  # never blocks on a lookup that is not coming
    assert [m.text for m in kick.detector.fed] == ["no refresh ran at all"]
    assert len(list((tmp_path / "streamer").glob("offline-*.jsonl"))) == 1
    s.close()
    kick.close()


def test_session_viewer_clip_of_a_near_miss_reaches_the_lane(tmp_path):
    from clipwatcher.detect.viewer_clips import ViewerClip
    from clipwatcher.models import Candidate

    near = Candidate("streamer", 2000.0, 1960.0, 2019.0, 3.5, 3.5)

    class Det(_Det):
        def rescue(self, moment_s):
            return [near] if abs(moment_s - 2000.0) < 30 else []

    class Poller:
        def __init__(self, s):
            self.session = s

        async def run(self, on_clip):
            on_clip(ViewerClip("other", 5000.0))
            on_clip(ViewerClip("abc", 2020.0))  # created at 2020: covers 1985-2020, the near miss

        def moment_times(self):
            return [2002.5]

    made, rescued = [], []
    live = twitch_replay.Session("777", 1_000_000, None, "4242")

    def make(s):
        made.append(s.user_id)
        return Poller(s)

    s = StreamSession(
        "streamer", root=tmp_path, make_detector=Det, lookup=lambda login: live, fetch=None, make_poller=make
    )
    s.on_rescue = rescued.append

    async def go():
        await s.refresh()
        await asyncio.sleep(0)  # let the poller task run

    asyncio.run(go())
    assert made == ["4242"] and rescued == [near] and s.moment_times() == [2002.5]
    s.close()


def test_session_has_no_clip_poller_without_helix_credentials(tmp_path, monkeypatch):
    from clipwatcher.session import helix_clip_poller

    live = twitch_replay.Session("777", 1_000_000, None, "4242")
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)
    assert helix_clip_poller(live) is None
    monkeypatch.setenv("TWITCH_CLIENT_ID", "id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "secret")
    poller = helix_clip_poller(live)
    assert poller.broadcaster_id == "4242" and poller.started_at == 1000.0
    assert helix_clip_poller(twitch_replay.Session("777", 1_000_000, None)) is None  # broadcaster unknown
