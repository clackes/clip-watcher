import asyncio
import json
from pathlib import Path

from clipwatcher import kickvod, vod
from clipwatcher.chat.kick import (
    KickChat,
    parse_pusher_frame,
    pusher_url,
    resolve_chatroom_id,
    strip_emotes,
)

DATA = {
    "id": "abc",
    "chatroom_id": 77,
    "content": "[emote:37226:KEKW] ahah [emote:1:Pog]",
    "created_at": "2024-01-01T00:00:01.500+00:00",
    "sender": {"id": 1, "username": "Foo_Bar", "slug": "foo-bar"},
}
FRAME = json.dumps(
    {"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.77.v2", "data": json.dumps(DATA)}
)


def test_conversion():
    assert strip_emotes("[emote:1:KEKW] x [emote:22:Pog]") == "KEKW x Pog"
    m = parse_pusher_frame(FRAME, {"foo_bar"})
    assert m.text == "KEKW ahah Pog" and m.platform == "kick" and m.is_bot
    assert m.user == "foo_bar" and m.ts_ms == 1704067201500
    assert parse_pusher_frame('{"event":"pusher:pong","data":"{}"}') is None
    assert parse_pusher_frame("not json") is None


def test_url_and_resolver():
    assert pusher_url().startswith("wss://ws-us2.pusher.com/app/") and "protocol=7" in pusher_url()
    seen = {}

    def fetch(url, headers):
        seen.update(url=url, headers=headers)
        return {"chatroom": {"id": 99}}

    assert resolve_chatroom_id("slug", fetch=fetch, headers_provider=lambda: {"Cookie": "cf=1"}) == 99
    assert seen["url"].endswith("/channels/slug") and seen["headers"]["Cookie"] == "cf=1"


def test_kickchat_with_fake_socket():
    sent = []

    class WS:
        def __init__(self):
            self.q = [json.dumps({"event": "pusher:ping", "data": {}}), FRAME]

        async def send(self, s):
            sent.append(json.loads(s))

        async def recv(self):
            return self.q.pop(0)

    class CM:
        async def __aenter__(self):
            return WS()

        async def __aexit__(self, *a):
            return False

    async def go():
        chat = KickChat("slug", chatroom_id=77, connect=lambda url: CM(), bots=set())
        async for m in chat:
            chat.close()
            return m

    m = asyncio.run(asyncio.wait_for(go(), 5))
    assert m.text == "KEKW ahah Pog"
    assert sent[0]["event"] == "pusher:subscribe" and sent[0]["data"]["channel"] == "chatrooms.77.v2"
    assert sent[1]["event"] == "pusher:pong"


def test_kickvod_builders():
    cmd = kickvod.build_ytdlp_cmd("https://kick.com/s/videos/u", 3725.2, 3760.0, Path("o.mp4"))
    assert cmd[0] == "yt-dlp" and "--download-sections" in cmd
    assert cmd[cmd.index("--download-sections") + 1] == "*01:02:05-01:02:40"
    assert cmd[-1] == "https://kick.com/s/videos/u" and "o.mp4" in cmd
    vid = "123e4567-e89b-12d3-a456-426614174000"
    assert kickvod.kick_vod_id(f"https://kick.com/s/videos/{vid.upper()}") == vid
    assert kickvod.vod_url("s", vid) == f"https://kick.com/s/videos/{vid}"


def test_download_chat_paging():
    start = 1704067200000
    pages = {
        "p1": ([{**DATA, "id": "1"}], "p2"),
        "p2": ([{**DATA, "id": "2", "created_at": "2024-01-01T00:00:09+00:00"}], None),
    }
    calls = []

    def page(iso):
        calls.append(iso)
        return pages.get("p2" if len(calls) > 1 else "p1") if len(calls) < 3 else ([], None)

    # first call returns cursor "p2" which is used as next start_time
    msgs = kickvod.download_chat(77, start, start + 60_000, page=page)
    assert [m.ts_ms for m in msgs] == [1500, 9000] and calls[1] == "p2"


def test_vod_platform_dispatch(monkeypatch):
    seen = {}
    monkeypatch.setattr(kickvod, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    assert vod.main(["--platform", "kick", "--vod", "x", "--streamer", "s"]) == 0
    assert seen["argv"][:2] == ["--platform", "kick"]


def test_history_chat_polls_dedupes_and_orders():
    import asyncio
    import json

    from clipwatcher.chat.kick import KickHistoryChat

    def item(i, ms):
        return {"id": f"m{i}", "type": "message", "content": f"hi [emote:1:KEKW] {i}",
                "metadata": json.dumps({"message_ref": str(ms)}), "created_at": "2026-11-17T00:00:00Z",
                "sender": {"slug": f"User{i}"}}  # fmt: skip

    pages = [
        {"data": {"messages": [item(2, 2000), item(1, 1000)]}},
        {"data": {"messages": [item(3, 3000), item(2, 2000)]}},
        {"data": {"messages": [item(9, 9000)]}},  # no overlap -> a gap
    ]
    calls = iter(pages)

    def fetch(url, headers):
        try:
            return next(calls)
        except StopIteration:
            chat.close()
            return {"data": {"messages": []}}

    async def no_sleep(_):
        return None

    chat = KickHistoryChat("x", channel_id=1, fetch=fetch, sleep=no_sleep, bots=set())
    chat.skip_backlog = False

    async def collect():
        return [m async for m in chat]

    got = asyncio.run(collect())
    assert [m.ts_ms for m in got] == [1000, 2000, 3000, 9000]
    assert got[0].user == "user1" and "KEKW" in got[0].text and got[0].platform == "kick"
    assert chat.gaps == 1


def test_history_message_with_null_metadata():
    from clipwatcher.chat.kick import history_message_to_chat

    m = {"id": "a", "type": "message", "content": "hey", "metadata": "null",
         "created_at": "2026-11-17T00:00:01Z", "sender": {"slug": "x"}}  # fmt: skip
    assert history_message_to_chat(m).ts_ms > 0
    m["metadata"] = None
    assert history_message_to_chat(m).user == "x"


def test_history_chat_skips_startup_backlog():
    """Regression: the first page is minutes-old backlog; firing on it clipped video we never recorded."""
    import asyncio

    from clipwatcher.chat.kick import KickHistoryChat

    def item(i):
        return {"id": f"m{i}", "type": "message", "content": "x", "metadata": None,
                "created_at": f"2026-11-17T00:00:0{i}Z", "sender": {"slug": "u"}}  # fmt: skip

    pages = iter([{"data": {"messages": [item(1), item(2)]}}, {"data": {"messages": [item(3), item(2)]}}])

    def fetch(url, headers):
        try:
            return next(pages)
        except StopIteration:
            chat.close()
            return {"data": {"messages": []}}

    async def no_sleep(_):
        return None

    chat = KickHistoryChat("x", channel_id=1, fetch=fetch, sleep=no_sleep, bots=set())

    async def collect():
        return [m async for m in chat]

    assert [m.text for m in asyncio.run(collect())] == ["x"]  # only m3
