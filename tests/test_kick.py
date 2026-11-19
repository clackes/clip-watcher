"""Kick: chat over the realtime websocket, its history, the polling fallback and the VOD lane.

FakeKick answers like the live service did on 19 Nov 2026: the channel page data, and a chat history read
backwards, 25 messages before a cursor in epoch microseconds.
"""

import asyncio
import json
import re
import time
from pathlib import Path

from clipwatcher import kickvod, vod
from clipwatcher.chat import kick
from clipwatcher.chat.kick import KickChat, KickHistoryChat, message_to_chat

EV = "App\\Events\\"


class FakeKick:
    def __init__(self, live=True):
        self.msgs: list[dict] = []  # oldest first
        self.calls: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self.live = live
        self.fail_lookups = 0

    def say(self, at_s, user="viewer", text="hi", ref_ms=None, mid=None):
        d = {
            "id": mid or f"m{len(self.msgs)}",
            "chat_id": 77,
            "content": text,
            "type": "message",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(at_s))),
            "metadata": json.dumps({"message_ref": str(ref_ms)}) if ref_ms is not None else None,
            "sender": {"id": 1, "username": user.title(), "slug": user},
            "_us": int(at_s) * 1_000_000 + len(self.msgs),  # where it sits in the history
        }
        self.msgs.append(d)
        return d

    def fetch(self, url, headers=None):
        self.calls.append(url)
        if "/channels/slug" in url and "/messages" not in url:
            if self.fail_lookups:
                self.fail_lookups -= 1
                raise OSError("403 from Cloudflare")
            ls = {"id": 555, "is_live": True, "start_time": "2026-11-19 20:11:45"} if self.live else None
            return {"id": 77, "slug": "slug", "chatroom": {"id": 99}, "livestream": ls}
        m = re.search(r"/channels/77/messages\?cursor=(\d+)", url)
        assert m, url
        older = sorted((d for d in self.msgs if d["_us"] < int(m.group(1))), key=lambda d: -d["_us"])
        page = older[: kick.PAGE]
        return {"data": {"messages": page, "cursor": page[-1]["_us"] if len(older) > len(page) else None}}

    def post(self, url, body):
        self.posts.append((url, body))
        if url.endswith("/chat/connection"):
            creds = {"url": "wss://realtime.example/connection/websocket"}
            return {
                "data": {
                    "mode": "websocket",
                    "connections": [{"provider": "centrifugo", "credentials": creds}],
                }
            }
        assert url.endswith("/auth/connection") and body["client_id"]
        return {"data": {"token": f"guest-{sum(u.endswith('/auth/connection') for u, _ in self.posts)}"}}


def push(event, data, channel="chatrooms.99.v2"):
    """One realtime publication, as Centrifugo frames it."""
    return json.dumps(
        {"push": {"channel": channel, "pub": {"data": {"event": event, "data": json.dumps(data)}}}}
    )


def chat_push(d):
    return push(EV + "ChatMessageEvent", {k: v for k, v in d.items() if k != "_us"})


class FakeWS:
    """A scripted websocket: each recv() gives the next item (raising it when it is an exception)."""

    def __init__(self, script):
        self.script, self.sent = list(script), []

    async def send(self, s):
        self.sent.append(s)

    async def recv(self):
        if not self.script:
            await asyncio.sleep(3600)
        item = self.script.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item


def sockets(*wss):
    """connect(url) for KickChat that hands out the given websockets in turn."""
    left = list(wss)

    def connect(url):
        ws = left.pop(0)
        if isinstance(ws, Exception):
            raise ws

        class CM:
            async def __aenter__(self):
                return ws

            async def __aexit__(self, *a):
                return False

        return CM()

    return connect


HELLO = json.dumps(
    {"id": 1, "connect": {"client": "c", "expires": True, "ttl": 2699, "ping": 25, "pong": True}}
)
SUBSCRIBED = json.dumps({"id": 2, "subscribe": {}})


async def _no_wait(_):
    return None


def collect(chat, n, timeout=5.0):
    async def go():
        out = []
        async for m in chat:
            out.append(m)
            if len(out) >= n:
                chat.close()
                break
        return out

    return asyncio.run(asyncio.wait_for(go(), timeout))


# ------------------------------------------------------------------------------------------ messages
def test_message_time_is_the_servers_not_the_viewers():
    """metadata.message_ref is the viewer's clock. On a real chat 6% were off by more than 5 s, some by an
    hour, some 40 s ahead; a stamp from the future made the detector drop the real chat after it."""
    k = FakeKick()
    at = 1_795_125_075  # created_at, whole seconds
    ok = message_to_chat(
        k.say(at, "foo-bar", "[emote:37226:KEKW] haha [emote:1:Pog]", ref_ms=at * 1000 + 420)
    )
    assert (ok.ts_ms, ok.user, ok.text, ok.platform) == (at * 1000 + 420, "foo-bar", "KEKW haha Pog", "kick")
    assert message_to_chat(k.say(at, ref_ms=(at + 40) * 1000)).ts_ms == at * 1000  # 40 s in the future
    assert message_to_chat(k.say(at, ref_ms=(at - 3623) * 1000)).ts_ms == at * 1000  # an hour behind
    assert message_to_chat(k.say(at)).ts_ms == at * 1000  # no stamp at all
    plus = {**k.say(at), "created_at": "2026-11-19T21:51:15+00:00"}  # how the websocket writes it
    assert message_to_chat(plus).ts_ms == 1_795_125_075_000
    assert message_to_chat({**k.say(at), "type": "celebration"}) is None
    assert message_to_chat(k.say(at, "botrix"), {"botrix"}).is_bot


def test_channel_and_live_session():
    k = FakeKick()
    ch = kick.channel("Slug", fetch=k.fetch)
    assert (ch.id, ch.chatroom_id, ch.stream_id, ch.started_ms) == (77, 99, "555", 1_795_119_105_000)
    s = kick.live_session("slug", fetch=k.fetch)
    assert (s.stream_id, s.started_ms, s.vod_id, s.platform) == ("555", 1_795_119_105_000, "77", "kick")
    assert kick.live_session("slug", fetch=FakeKick(live=False).fetch) is None


# ------------------------------------------------------------------------------------------- history
def test_replay_reads_a_stretch_backwards_in_parallel_slices():
    k = FakeKick()
    t0 = 1_795_120_000
    for i in range(2400):  # 40 minutes, a message a second
        k.say(t0 + i, f"u{i % 50}", f"line {i}")
    msgs, reached = kick.replay(77, (t0 + 300) * 1000, (t0 + 2099) * 1000, fetch=k.fetch, bots=set())
    assert reached == (t0 + 300) * 1000 and [m.text for m in msgs] == [f"line {i}" for i in range(300, 2100)]
    one, _ = kick.replay(77, (t0 + 300) * 1000, (t0 + 2099) * 1000, fetch=k.fetch, bots=set(), workers=1)
    assert [m.text for m in one] == [m.text for m in msgs]  # same as one reader going all the way back


def test_replay_under_a_budget_returns_the_newest_unbroken_stretch():
    k = FakeKick()
    t0 = 1_795_120_000
    for i in range(3000):
        k.say(t0 + i, text=f"line {i}")
    ticks = iter(range(10_000))  # every look at the clock is a second later: the budget runs out mid-read
    msgs, reached = kick.replay(
        77, t0 * 1000, (t0 + 2999) * 1000, budget_s=40, fetch=k.fetch, bots=set(), clock=lambda: next(ticks)
    )
    got = [int(m.text.split()[1]) for m in msgs]
    assert 0 < len(got) < 3000 and got == list(range(got[0], 3000))  # ends at the end, no hole in it
    assert reached == (t0 + got[0]) * 1000 > t0 * 1000  # and says how far back it got


def test_poll_follows_the_history_back_until_it_joins_what_it_knows():
    """The newest page is 25 messages. A chat faster than that between two polls lost the rest."""
    k = FakeKick()
    now = int(time.time())
    for i in range(10):
        k.say(now - 60 + i, text=f"old {i}")
    seen = kick._Seen()
    for d in k.msgs:
        seen.fresh(d["id"])
    for i in range(70):  # 70 new messages before the next poll: almost three pages
        k.say(now - 10 + i // 10, text=f"new {i}")
    new, joined = kick.poll_new(77, seen, fetch=k.fetch)
    assert joined and [d["content"] for _, d in new] == [f"new {i}" for i in range(70)]
    assert "cursor=" in k.calls[0]  # the bare address is cached for 10 s by Kick's CDN: always a fresh cursor
    late, joined = kick.poll_new(77, kick._Seen(), fetch=k.fetch, since_ms=(now - 5) * 1000)
    assert joined and all(kick.created_ms(d) >= (now - 5) * 1000 for _, d in late) and late  # no backlog


# ----------------------------------------------------------------------------------------- live chat
def test_kickchat_speaks_centrifugo():
    k = FakeKick()
    now = int(time.time())
    gift = {"gifter_username": "Whale", "gifted_total": 20, "gifted_usernames": ["a", "b"]}
    host = {"user": {"username": "BigStreamer"}, "message": {"numberOfViewers": 1500}}
    ws = FakeWS([
        HELLO, SUBSCRIBED, "{}",
        chat_push(k.say(now, "viewer", "KEKW")) + "\n" + chat_push(k.say(now, "other", "LMAO")),  # one frame
        push("GiftedSubscriptionsEvent", gift, "chatroom_99"),
        push("GiftedSubscriptionsEvent", {**gift, "chunk_details": {"chunk_index": 1}}, "chatroom_99"),
        push(EV + "StreamHostedEvent", host, "chatrooms.99"),
        push(EV + "MessageDeletedEvent", {"id": "x", "message": {"id": "m0"}}),
    ])  # fmt: skip
    chat = KickChat("Slug", connect=sockets(ws), fetch=k.fetch, post=k.post, bots=set())
    got = collect(chat, 4)
    assert [(m.user, m.text) for m in got[:2]] == [("viewer", "KEKW"), ("other", "LMAO")]
    assert (got[2].notice_type, got[2].viewers, got[2].user) == (
        "submysterygift",
        20,
        "whale",
    )  # counted once
    assert (got[3].notice_type, got[3].viewers) == ("raid", 1500)
    sent = [json.loads(s) for s in ws.sent]
    assert sent[0] == {"id": 1, "connect": {"token": "guest-1", "name": "js"}}
    assert sent[1] == {"id": 2, "subscribe": {"channel": "chatrooms.99.v2"}}
    assert {s["subscribe"]["channel"] for s in sent[2:5]} == {"chatroom_99", "chatrooms.99", "channel_77"}
    assert sent[5] == {}  # the server's ping answered
    assert k.posts[0][0].endswith("/realtime/channels/77/chat/connection") and chat.connects == 1


def test_kickchat_reconnect_reads_what_it_missed_from_the_history():
    k = FakeKick()
    now = int(time.time())
    m1 = k.say(now - 20, text="one")
    first = FakeWS([HELLO, SUBSCRIBED, chat_push(m1), ConnectionError("line dropped")])
    m2, m3 = k.say(now - 15, text="two"), k.say(now - 12, text="three")  # said while we were away
    m4 = k.say(now - 2, text="four")
    second = FakeWS([HELLO, SUBSCRIBED, chat_push(m3), chat_push(m4)])  # m3 again: in the history and live
    chat = KickChat(
        "slug", connect=sockets(first, second), fetch=k.fetch, post=k.post, bots=set(), sleep=_no_wait
    )
    got = collect(chat, 4)
    assert [m.text for m in got] == ["one", "two", "three", "four"]  # nothing lost, nothing twice, in order
    assert chat.connects == 2 and chat.filled >= 2 and m2["content"] == "two"  # read back from the history


def test_kickchat_renews_its_guest_token_on_the_open_connection():
    k = FakeKick()
    now = [1000.0]
    msg = k.say(int(time.time()), text="hello")

    def later():
        now[0] += 60  # past ttl - 120 s
        return chat_push(msg)

    hello = json.dumps({"id": 1, "connect": {"expires": True, "ttl": 150, "ping": 25}})
    ws = FakeWS([hello, SUBSCRIBED, later, chat_push(k.say(int(time.time()), text="again"))])
    chat = KickChat("slug", connect=sockets(ws), fetch=k.fetch, post=k.post, bots=set(), clock=lambda: now[0])
    assert [m.text for m in collect(chat, 2)] == ["hello", "again"]
    assert {"id": 100, "refresh": {"token": "guest-2"}} in [json.loads(s) for s in ws.sent]


def test_kickchat_without_a_websocket_polls_the_history_and_skips_the_backlog():
    k = FakeKick()
    now = int(time.time())
    for i in range(40):
        k.say(now - 600 + i, text=f"backlog {i}")  # from before we joined: the session reads history, not us
    naps = []

    async def nap(_):
        naps.append(1)
        if len(naps) == 3:  # after the first poll a viewer says something
            k.say(int(time.time()), text="fresh")

    blocked = [OSError("websocket blocked")] * 4
    chat = KickChat(
        "slug", connect=sockets(*blocked), fetch=k.fetch, post=k.post, bots=set(), sleep=nap, ws_fail_max=2,
    )  # fmt: skip
    got = collect(chat, 1)
    assert [m.text for m in got] == ["fresh"] and chat.polled == 1 and chat.connects == 0


def test_history_chat_skips_startup_backlog_and_follows_fast_chat():
    k = FakeKick()
    now = int(time.time())
    for i in range(30):
        k.say(now - 100 + i, text=f"backlog {i}")
    polls = []

    async def nap(_):
        polls.append(1)
        if len(polls) == 1:
            for i in range(60):  # more than two pages before the next poll
                k.say(now - 5 + i // 20, text=f"new {i}")
        elif len(polls) >= 3:
            chat.close()

    chat = KickHistoryChat("slug", fetch=k.fetch, sleep=nap, bots=set())

    async def run():
        return [m async for m in chat]

    got = asyncio.run(asyncio.wait_for(run(), 5))
    assert (
        [m.text for m in got] == [f"new {i}" for i in range(60)] and chat.gaps == 0 and chat.channel_id == 77
    )


def test_history_chat_retries_the_channel_lookup():
    """A failed lookup raised out of the chat iterator and took the lane and its recorder down with it."""
    k = FakeKick()
    k.fail_lookups = 2

    async def nap(_):
        if chat.channel_id is not None:  # found on the third try, and the first poll went through
            chat.close()

    chat = KickHistoryChat("slug", fetch=k.fetch, sleep=nap, bots=set())

    async def run():
        return [m async for m in chat]

    assert asyncio.run(asyncio.wait_for(run(), 5)) == [] and chat.channel_id == 77
    assert sum("/channels/slug" in u for u in k.calls) == 3


# ------------------------------------------------------------------------------------------ VOD lane
VIDEOS = [
    {
        "id": 138291484, "channel_id": 77, "start_time": "2026-11-17 01:27:56", "duration": 3_239_000,
        "is_live": False, "source": "https://stream.kick.com/abc/media/hls/master.m3u8",
        "video": {"id": 134, "uuid": "d2c5e86a-ddb8-43bb-bc91-45647ae4f485"},
    },
    {"id": 1, "channel_id": 77, "start_time": "2026-11-18 01:00:00", "duration": 0, "video": {"uuid": "x"}},
]  # fmt: skip
MASTER = """#EXTM3U
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=8972224,RESOLUTION=1920x1080,VIDEO="1080p60",FRAME-RATE=60.000
1080p60/playlist.m3u8
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=3489511,RESOLUTION=1280x720,VIDEO="720p60",FRAME-RATE=60.000
720p60/playlist.m3u8
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=630000,RESOLUTION=640x360,VIDEO="360p30",FRAME-RATE=30.000
360p30/playlist.m3u8
"""


def test_kickvod_finds_the_vod_in_the_channels_list_and_cuts_from_its_playlist():
    uuid = "d2c5e86a-ddb8-43bb-bc91-45647ae4f485"
    assert kickvod.kick_vod_id(f"https://kick.com/s/videos/{uuid.upper()}") == uuid
    asked = []
    meta = kickvod.fetch_vod_meta("Slug", uuid, get=lambda url: asked.append(url) or VIDEOS)
    assert asked == ["https://kick.com/api/v2/channels/slug/videos"]  # /api/v1/video/<uuid> answers 403
    assert (meta["start_ms"], meta["duration_s"], meta["channel_id"]) == (1_794_878_876_000, 3239.0, 77)
    try:
        kickvod.fetch_vod_meta("slug", "not-listed", get=lambda url: VIDEOS)
    except LookupError:
        pass
    else:
        raise AssertionError("an unlisted VOD must say so")
    src = meta["source"]
    assert (
        kickvod.pick_variant(src, MASTER, "720p60")
        == "https://stream.kick.com/abc/media/hls/720p60/playlist.m3u8"
    )
    assert kickvod.pick_variant(src, MASTER, "1440p60").endswith("/1080p60/playlist.m3u8")  # else the largest
    assert kickvod.pick_variant(src, "#EXTM3U\n", "1080p60") == src
    cmd = kickvod.build_cut_cmd("https://x/playlist.m3u8", 3725.2, 3796.2, Path("o.mp4"), ffmpeg="ffmpeg")
    assert cmd[cmd.index("-ss") + 1] == "3725.200" and cmd.index("-ss") < cmd.index("-i")  # seek, then exact
    assert cmd[cmd.index("-t") + 1] == "71.000" and "libx264" in cmd and cmd[-1] == "o.mp4"


def test_kickvod_chat_counts_from_the_vod_start():
    k = FakeKick()
    start = 1_794_878_876
    for i in range(0, 600, 3):
        k.say(start + i, text=f"line {i}")
    k.say(start - 50, text="before the stream")
    msgs = kickvod.download_chat(77, start * 1000, (start + 300) * 1000, fetch=k.fetch)
    assert [m.ts_ms for m in msgs] == [i * 1000 for i in range(0, 301, 3)] and msgs[0].text == "line 0"


def test_vod_platform_dispatch(monkeypatch):
    seen = {}
    monkeypatch.setattr(kickvod, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    assert vod.main(["--platform", "kick", "--vod", "x", "--streamer", "s"]) == 0
    assert seen["argv"][:2] == ["--platform", "kick"]


# -------------------------------------------------------------------------------- limits, clips, status
def test_a_block_by_kick_is_waited_out_by_every_caller(monkeypatch):
    """Kick answers 429 to an address that asks too much. The first long history read at 4 requests a second
    was blocked after 80 s and gave up 20 s later; a block lifts within 5 s when nobody keeps asking."""
    import io
    import urllib.error

    answers = [429, 429, 200]
    naps, asked = [], []

    def urlopen(req, timeout=0):
        asked.append(req.full_url)
        code = answers.pop(0)
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "Too Many Requests", {}, io.BytesIO(b"{}"))
        return io.BytesIO(b'{"data": {"messages": []}}')

    monkeypatch.setattr(kick.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(kick.time, "sleep", naps.append)
    monkeypatch.setattr(kick, "_LIMITER", kick._Limiter(1000.0))
    assert kick._http_json("https://kick.com/api/v2/channels/77/messages?cursor=1") == {
        "data": {"messages": []}
    }
    assert len(asked) == 3 and sum(naps) > 10  # two blocks, each waited out (6 s) before asking again
    answers[:] = [403]
    try:
        kick._http_json("https://kick.com/api/v1/video/x")
    except urllib.error.HTTPError as e:
        assert e.code == 403  # anything but a block is the caller's to handle, at once
    else:
        raise AssertionError("403 must not be swallowed")


def test_kick_clips_are_read_without_a_login_and_know_their_length():
    from clipwatcher.detect.viewer_clips import KickClipPoller

    page = {
        "clips": [
            {
                "id": "clip_A",
                "livestream_id": "555",
                "duration": 53,
                "created_at": "2026-11-19T21:45:32.200083Z",
            },
            {"id": "clip_B", "livestream_id": "111", "duration": 30, "created_at": "2026-11-18T10:00:00Z"},
            {"id": "clip_C", "livestream_id": "555", "created_at": "not a time"},
        ],
        "nextCursor": None,
    }
    asked = []
    p = KickClipPoller("Slug", "555", get=lambda url: asked.append(url) or page)
    new = p.poll_once()
    assert [c.id for c in new] == ["clip_A"] and p.poll_once() == []  # this broadcast only, each clip once
    assert asked[0].startswith("https://kick.com/api/v2/channels/slug/clips?") and "sort=date" in asked[0]
    a, b = new[0].window
    assert round(b - a) == 53 + 30 and round(b) == 1_795_124_732  # its length, plus the time to save it


def test_kick_live_status_without_credentials(monkeypatch):
    from clipwatcher.ingest.watcher import KickPoller

    monkeypatch.delenv("KICK_CLIENT_ID", raising=False)
    monkeypatch.delenv("KICK_CLIENT_SECRET", raising=False)
    live = {
        "id": 138939992,
        "is_live": True,
        "start_time": "2026-11-19 20:11:45",
        "viewer_count": 37286,
        "session_title": "GTA VI GRIND",
        "categories": [{"name": "Grand Theft Auto VI (GTA)"}],
    }

    def fetch(url, headers):
        if url.endswith("/channels/gone"):
            raise OSError("404")
        return {"id": 1, "livestream": live if url.endswith("/channels/n3on") else None}

    st = KickPoller(fetch=fetch).poll(["N3on", "adinross", "gone"])
    assert st["n3on"] == {
        "live": True, "stream_id": "138939992", "viewers": 37286, "title": "GTA VI GRIND",
        "game": "Grand Theft Auto VI (GTA)", "started_at": "2026-11-19T20:11:45Z",
    }  # fmt: skip
    assert (
        not st["adinross"]["live"] and not st["gone"]["live"]
    )  # an unknown channel does not hide the others


def test_kick_vod_clips_are_placed_on_the_vod_timeline():
    from clipwatcher.detect.viewer_clips import kick_vod_clip_moments

    start_ms = 1_795_119_105_000  # 2026-11-19 20:11:45
    page = {
        "clips": [
            {"id": "a", "livestream_id": "555", "duration": 60, "created_at": "2026-11-19T21:45:35Z"},
            {"id": "b", "livestream_id": "other", "duration": 60, "created_at": "2026-11-19T21:00:00Z"},
        ]
    }
    asked = []
    got = kick_vod_clip_moments("Slug", "555", start_ms, get=lambda url: asked.append(url) or page)
    # saved 5630 s into the stream: it covers 5540-5630 (60 s of clip, up to 30 s to save it), middle 5585
    assert got == [5585.0] and "sort=view" in asked[0] and "/channels/slug/clips" in asked[0]
    assert kick_vod_clip_moments("slug", "555", start_ms, get=lambda url: 1 / 0) == []
