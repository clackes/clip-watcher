import asyncio
import json

from clipwatcher.chat.live_log import LiveLog
from clipwatcher.chat.twitch_irc import TwitchChat, line_to_message, parse_line
from clipwatcher.detect.detector import confirm, detect
from clipwatcher.detect.live import LiveDetector
from clipwatcher.detect.viewer_clips import ViewerClipPoller, clip_window, parse_clips
from clipwatcher.detect.viewer_clips import confirm as vc_confirm
from clipwatcher.models import ChatMessage
from tests.fixtures import synth

PRIV = (
    "@badge-info=;display-name=Lucas_Reed;tmi-sent-ts=1700000000123;msg-id=x "
    ":lucas_reed!lucas_reed@lucas_reed.tmi.twitch.tv PRIVMSG #chan :KEKW so funny"
)
RAID = (
    "@login=raider;msg-id=raid;msg-param-viewerCount=250;tmi-sent-ts=1700000001000 "
    ":tmi.twitch.tv USERNOTICE #chan"
)


def test_parse_privmsg_and_tags():
    ln = parse_line(PRIV)
    assert ln.command == "PRIVMSG" and ln.params == ["#chan", "KEKW so funny"]
    m = line_to_message(ln, {"nightbot"})
    assert (m.ts_ms, m.user, m.text, m.is_bot) == (1700000000123, "lucas_reed", "KEKW so funny", False)
    bot = line_to_message(parse_line(PRIV.replace("Lucas_Reed", "Nightbot")), {"nightbot"})
    assert bot.is_bot


def test_tag_escapes_and_action():
    ln = parse_line("@display-name=a\\sb;tmi-sent-ts=5 :x!x@x PRIVMSG #c :\x01ACTION waves\x01")
    assert ln.tags["display-name"] == "a b"
    assert line_to_message(ln).text == "waves"


def test_usernotice_raid():
    m = line_to_message(parse_line(RAID))
    assert m.is_notice and m.notice_type == "raid" and m.viewers == 250 and m.user == "raider"
    gift = "@login=g;msg-id=submysterygift;msg-param-mass-gift-count=20;tmi-sent-ts=1 :t USERNOTICE #c"
    assert line_to_message(parse_line(gift)).notice_type == "submysterygift"


def test_ping_reconnect_parse():
    assert parse_line("PING :tmi.twitch.tv").command == "PING"
    assert parse_line(":tmi.twitch.tv RECONNECT").command == "RECONNECT"
    assert line_to_message(parse_line("PING :x")) is None


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, 15))


def test_irc_client_against_fake_server():
    seen: list[str] = []
    state = {"conns": 0}

    async def handler(reader, writer):
        state["conns"] += 1
        n = state["conns"]
        for _ in range(4):
            seen.append((await reader.readline()).decode().strip())
        if n == 1:
            writer.write(f"{PRIV}\r\nPING :tmi.twitch.tv\r\n".encode())
            await writer.drain()
            pong = (await reader.readline()).decode().strip()
            seen.append(pong)
            writer.write(b":tmi.twitch.tv RECONNECT\r\n")
            await writer.drain()
        else:
            writer.write(f"{RAID}\r\n".encode())
            await writer.drain()
            await asyncio.sleep(0.3)
        writer.close()

    async def go():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        chat = TwitchChat("Chan", host="127.0.0.1", port=port, use_tls=False, bots=set(), backoff_base=0.01)
        out = []
        async for m in chat:
            out.append(m)
            if len(out) == 2:
                chat.close()
        server.close()
        return out, chat

    out, chat = _run(go())
    assert [m.text for m in out] == ["KEKW so funny", ""] and out[1].notice_type == "raid"
    assert chat.connects == 2
    assert seen[0] == "CAP REQ :twitch.tv/tags twitch.tv/commands"
    assert seen[1] == "PASS SCHMOOPIIE" and seen[2].startswith("NICK justinfan") and seen[3] == "JOIN #chan"
    assert "PONG :tmi.twitch.tv" in seen


def test_irc_pong_timeout_reconnects():
    state = {"conns": 0}

    async def handler(reader, writer):
        state["conns"] += 1
        for _ in range(4):
            await reader.readline()
        if state["conns"] == 2:
            writer.write(f"{PRIV}\r\n".encode())
            await writer.drain()
        await asyncio.sleep(1)  # never answers PING on the first connection
        writer.close()

    async def go():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        chat = TwitchChat(
            "c", host="127.0.0.1", port=port, use_tls=False, bots=set(),
            ping_interval=0.1, pong_timeout=0.1, backoff_base=0.01,
        )  # fmt: skip
        async for m in chat:
            chat.close()
            server.close()
            return m, chat

    m, chat = _run(go())
    assert m.user == "lucas_reed" and chat.connects == 2


def test_live_log_flushes_each_line(tmp_path):
    log = LiveLog("Chan", "s1", root=tmp_path)
    log.write(ChatMessage(ts_ms=1, user="a", text="hi"))
    lines = log.path.read_text().splitlines()  # visible before close
    assert json.loads(lines[0])["text"] == "hi" and log.path == tmp_path / "chan" / "s1.jsonl"
    log.close()


def test_live_detector_matches_batch():
    base = 1_700_000_000
    msgs = synth.make_chat()
    batch = detect(msgs, streamer="examplestreamer", top=8, before_s=20, after_s=10, gap_s=60)
    det = LiveDetector("examplestreamer")
    det.warmup_s = 60  # the synthetic chat spikes within the first 10 min (live warm-up)
    live = []
    for m in msgs:
        c = det.feed(ChatMessage(**{**m.to_dict(), "ts_ms": m.ts_ms + base * 1000}))
        if c:
            live.append(c)
    live += det.drain()
    peaks = [c.peak_s - base for c in live]
    assert len(live) == len(batch) == 3, (peaks, [c.peak_s for c in batch])
    for b in batch:
        assert any(abs(p - b.peak_s) <= 6 for p in peaks), (b.peak_s, peaks)
    assert all(c.end_s > c.start_s and c.unique_chatters >= 8 for c in live)
    assert not any(synth.RAID_AT - 20 <= p <= synth.RAID_AT + 90 for p in peaks)


def test_live_detector_warms_up_and_primes_from_logged_chat():
    base = 1_700_000_000_000
    calm = [ChatMessage(base + t * 1000, f"u{t % 40}", f"talking {t}") for t in range(400)]
    spike = [ChatMessage(base + 400_000 + i * 100, f"fan{i}", "KEKW LMAO") for i in range(80)]
    cold = LiveDetector("nobody")
    cold.warmup_s = 600
    fired = [c for m in calm + spike if (c := cold.feed(m))] + cold.drain()
    assert fired == []  # 400 s in: still learning this chat
    warm = LiveDetector("nobody")
    warm.warmup_s = 600
    assert warm.prime([ChatMessage(m.ts_ms - 600_000, m.user, m.text) for m in calm]) == 400
    fired = [c for m in calm + spike if (c := warm.feed(m))] + warm.drain()
    assert len(fired) == 1  # the same spike after a restart that primed the baseline


def test_live_detector_tick_finalizes_quiet_chat():
    det = LiveDetector("x")
    assert det.feed(ChatMessage(ts_ms=1_000_000, user="a", text="hi")) is None
    assert det.tick(1_000_100) is None


def test_viewer_clip_window_and_poll():
    assert clip_window(1000.0) == (965.0, 1000.0)
    payload = {"data": [{"id": "A", "created_at": "2024-01-01T00:10:00Z", "url": "u"}, {"id": "bad"}]}
    clips = parse_clips(payload)
    assert len(clips) == 1 and clips[0].window[1] - clips[0].window[0] == 35
    urls = []
    p = ViewerClipPoller(
        "42", 1_704_067_200.0, client_id="cid", token="t", get=lambda u: urls.append(u) or payload
    )
    assert len(p.poll_once()) == 1 and p.poll_once() == []
    assert "broadcaster_id=42" in urls[0] and "started_at=2024-01-01T00%3A00%3A00Z" in urls[0]
    cand = detect(synth.make_chat(), streamer="x", top=1)[0]
    from clipwatcher.detect.viewer_clips import ViewerClip

    vc = ViewerClip("z", cand.peak_s + 20)
    out = vc_confirm(cand, [vc])
    assert out.signals["viewer_clip"] == 1.0 and out.final_score > cand.chat_score
    assert (
        confirm(cand, [cand.peak_s + 5]).final_score
        == vc_confirm(cand, [ViewerClip("y", cand.peak_s + 22.5)]).final_score
    )


def test_frequent_bot_messages_do_not_block_fires():
    """Regression: big channels have bots posting every few seconds; holding every fire for 20 s after
    any bot message dropped real spikes (replay missed 4 of 10 top moments)."""
    from clipwatcher.detect.live import LiveDetector
    from clipwatcher.models import ChatMessage

    base = 1_790_000_000_000
    msgs = []
    for t in range(0, 1200):  # 20 min: 1 msg/s background from 40 users, nightbot every 5 s
        msgs.append(ChatMessage(base + t * 1000, f"u{t % 40}", f"talking {t}"))
        if t % 5 == 0:
            msgs.append(ChatMessage(base + t * 1000 + 1, "nightbot", "follow on twitter!", is_bot=True))
    for i in range(80):  # spike at 1000 s: 80 different chatters laughing within 8 s
        msgs.append(ChatMessage(base + 1000_000 + i * 100, f"fan{i}", "KEKW LMAO"))
    msgs.sort(key=lambda m: m.ts_ms)
    det = LiveDetector("nobody")
    fired = [c for m in msgs if (c := det.feed(m))] + det.drain()
    assert any(abs(c.signals["fire_s"] - (base / 1000 + 1000)) < 15 for c in fired)
    assert max(c.chat_score for c in fired) >= 5


def test_streamer_prompted_copy_paste_is_not_a_moment():
    """Regression from a live run: chat typing IWASHERE / yo on request fired a clip of nothing."""
    from clipwatcher.detect.live import LiveDetector
    from clipwatcher.models import ChatMessage

    base = 1_790_000_000_000

    def run(burst_text):
        msgs = [ChatMessage(base + t * 1000, f"u{t % 40}", f"talking {t}") for t in range(1200)]
        for i in range(90):
            text = burst_text[i % len(burst_text)]
            msgs.append(ChatMessage(base + 1000_000 + i * 100, f"fan{i}", text))
        msgs.sort(key=lambda m: m.ts_ms)
        det = LiveDetector("nobody")
        return [c for m in msgs if (c := det.feed(m))] + det.drain()

    assert not [c for c in run(["IWASHERE", "IWASHERE", "yo"]) if c.chat_score >= 5]
    assert [c for c in run(["KEKW", "LMAO", "no way", "OMEGALUL"]) if c.chat_score >= 5]


def test_small_gifted_subs_do_not_mute_the_detector():
    """Regression (GTA VI launch, 18 Nov): every 1-3 sub community gift muted the detector for 75 s, so xqc's
    was deaf 99% of the stream. Only a real sub-bomb (>= detector.sub_bomb_min gifts) mutes."""
    base = 1_790_000_000_000

    def run(gift_count):
        msgs = [ChatMessage(base + t * 1000, f"u{t % 40}", f"talking {t}") for t in range(1200)]
        for t in range(0, 1200, 20):  # a community gift every 20 s, with its subgift notices
            msgs.append(ChatMessage(base + t * 1000 + 1, "gifter", "", is_notice=True,
                                    notice_type="submysterygift", viewers=gift_count))  # fmt: skip
            for k in range(gift_count):
                msgs.append(ChatMessage(base + t * 1000 + 2 + k, "gifter", "", is_notice=True,
                                        notice_type="subgift"))  # fmt: skip
        for i in range(80):  # the moment at 1000 s
            msgs.append(ChatMessage(base + 1_000_000 + i * 100, f"fan{i}", "KEKW LMAO"))
        msgs.sort(key=lambda m: m.ts_ms)
        det = LiveDetector("nobody")
        return [c for m in msgs if (c := det.feed(m))] + det.drain()

    assert any(abs(c.signals["fire_s"] - (base / 1000 + 1000)) < 15 for c in run(3))
    assert not run(20)  # a 20-sub bomb every 20 s keeps it muted


def test_spike_without_reaction_does_not_fire(monkeypatch):
    """Owner, 18 Nov: chat spikes of prompted words ("TIP TIP" in a GTA strip club, emote spam) are not
    moments; the same spike of laughter is (detector.min_reaction_share)."""
    from clipwatcher import config

    real = config.global_config()
    monkeypatch.setattr(config, "global_config", lambda: {**real, "detector": {**real["detector"],
                                                          "min_reaction_share": 0.15}})  # fmt: skip
    base = 1_790_000_000_000

    def run(word):
        msgs = [ChatMessage(base + t * 1000, f"u{t % 40}", f"talking {t}") for t in range(1200)]
        msgs += [ChatMessage(base + 1_000_000 + i * 100, f"fan{i}", word) for i in range(80)]
        msgs.sort(key=lambda m: m.ts_ms)
        det = LiveDetector("nobody")
        return [c for m in msgs if (c := det.feed(m))] + det.drain()

    assert any(abs(c.signals["fire_s"] - (base / 1000 + 1000)) < 15 for c in run("KEKW LMAO"))
    assert not run("TIP HER")


def test_windows_tls_uses_certifi_roots(monkeypatch):
    import importlib
    import os

    import pytest

    import clipwatcher

    certifi = pytest.importorskip("certifi")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(os, "name", "nt")
    importlib.reload(clipwatcher)
    assert os.environ["SSL_CERT_FILE"] == certifi.where()
