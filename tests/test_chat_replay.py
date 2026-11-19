"""D13: the chat of a broadcast from its first second (chat/twitch_replay.py) and kept whole on disk
(chat/live_log.py)."""

import json
import time

from clipwatcher.chat import twitch_replay
from clipwatcher.chat.live_log import LiveLog
from clipwatcher.models import ChatMessage


# ---------------------------------------------------------------------------------------- the replay
def _vod(comments, length):
    """Fake GQL: comments = [(offset_s, id, login, text)], served like Twitch (page of 3 from offset - 2)."""

    def post(payload):
        v = payload["variables"]
        if "login" in v:
            if v["login"] != "streamer":
                return {"data": {"user": None}}
            stream = {"id": "777", "createdAt": "2026-11-19T18:00:00Z", "archiveVideo": {"id": "42"}}
            return {"data": {"user": {"stream": stream}}}
        if v["id"] != "42":
            return {"data": {"video": None}}
        if v["off"] > length:
            return {
                "data": {"video": {"lengthSeconds": length, "comments": None}},
                "errors": [{"message": "x"}],
            }
        page = [c for c in comments if c[0] >= v["off"] - 2][:3]
        edges = [
            {
                "node": {
                    "id": c[1],
                    "contentOffsetSeconds": c[0],
                    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_795_111_200 + c[0]))
                    + ".250Z",
                    "commenter": {"login": c[2]},
                    "message": {"fragments": [{"text": c[3]}]},
                }
            }
            for c in page
        ]
        return {"data": {"video": {"lengthSeconds": length, "comments": {"edges": edges}}}}

    return post


def test_replay_session_lookup():
    post = _vod([], 0)
    s = twitch_replay.live_session("Streamer", post=post)
    assert s == twitch_replay.Session("777", 1_795_111_200_000, "42")
    assert twitch_replay.live_session("offline_or_unknown", post=post) is None


def test_replay_pages_by_offset_and_recognises_notices():
    comments = [(t, f"id{t}", f"user{t}", f"msg {t}") for t in range(0, 200, 3)]
    comments += [(50, f"dense{i}", f"d{i}", "KEKW") for i in range(7)]  # one second busier than a page
    comments += [
        (120, "raid", "raider", "15 raiders from Raider have joined!"),
        (130, "bomb", "gifter", "Gifter is gifting 20 Tier 1 Subs to Streamer's community!"),
        (140, "sub", "viewer", "Viewer subscribed with Prime. They've subscribed for 4 months! hi"),
        (150, "bot", "nightbot", "follow the socials"),
    ]
    comments.sort()
    msgs = twitch_replay.fetch("42", 0, 400, post=_vod(comments, 205), bots={"nightbot"}, workers=1)
    assert len({(m.ts_ms, m.user) for m in msgs}) == len(msgs)  # no duplicates across pages
    assert len(msgs) >= len(comments) - 4  # a second denser than a page loses its overflow, nothing else
    assert msgs == sorted(msgs, key=lambda m: m.ts_ms) and msgs[0].ts_ms == 1_795_111_200_250
    by = {m.user: m for m in msgs}
    assert (by["raider"].notice_type, by["raider"].viewers) == ("raid", 15)
    assert (by["gifter"].notice_type, by["gifter"].viewers) == ("submysterygift", 20)
    assert by["viewer"].is_notice and by["nightbot"].is_bot and not by["user3"].is_notice
    assert twitch_replay.fetch("gone", 0, 100, post=_vod(comments, 205)) == []
    part = twitch_replay.fetch("42", 60, 90, post=_vod(comments, 205), workers=1)
    assert part and all(1_795_111_260_000 <= m.ts_ms < 1_795_111_290_999 for m in part)


# --------------------------------------------------------------------------------- the persistent log
def test_live_log_reads_back_and_survives_a_torn_line(tmp_path):
    log = LiveLog("Chan", "777", root=tmp_path)
    log.write(ChatMessage(2000, "b", "second"))
    log.write(ChatMessage(3000, "c", "third"))
    with log.path.open("a") as f:
        f.write('{"ts_ms": 4000, "user": "d", "te')  # crash mid-write
    assert [m.text for m in log.read()] == ["second", "third"]
    log.rewrite([ChatMessage(1000, "a", "first"), *log.read()])
    log.write(ChatMessage(5000, "e", "live"))
    assert [json.loads(line)["text"] for line in log.path.read_text().splitlines()] == [
        "first", "second", "third", "live",
    ]  # fmt: skip
    log.close()
