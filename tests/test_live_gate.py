import asyncio

from clipwatcher import live

GTA = ["Grand Theft Auto VI"]


def _seq(values):
    it = iter(values)
    calls = []

    def look(platform, login):
        calls.append(login)
        return next(it)

    return look, calls


async def _nosleep(s):
    return None


def test_waits_until_the_stream_is_gta6_not_gta5():
    look, calls = _seq([None, "Grand Theft Auto V", "Just Chatting", "Grand Theft Auto VI"])
    got = asyncio.run(live.wait_for_game("twitch", "kaicenat", GTA, look=look, sleep=_nosleep))
    assert got == "Grand Theft Auto VI" and len(calls) == 4


def test_stops_on_a_game_switch_or_a_long_offline():
    look, _ = _seq(["Grand Theft Auto VI", None, "Grand Theft Auto VI", "Grand Theft Auto V"])
    assert asyncio.run(live.left_game("twitch", "x", GTA, look=look, sleep=_nosleep)) == "Grand Theft Auto V"
    look, _ = _seq(["Grand Theft Auto VI", None, None, None])
    assert asyncio.run(live.left_game("twitch", "x", GTA, look=look, sleep=_nosleep)) == "offline"
