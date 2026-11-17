import asyncio

from clipwatcher import live


def test_supervise_restarts_after_crash(monkeypatch):
    calls = []

    async def boom(login, **kw):
        calls.append(login)
        if len(calls) >= 3:
            raise asyncio.CancelledError
        raise RuntimeError("streamlink died")

    async def fast_sleep(_):
        return None

    monkeypatch.setattr(live, "run_streamer", boom)
    monkeypatch.setattr(live.asyncio, "sleep", fast_sleep)
    try:
        asyncio.run(live.supervise("x"))
    except asyncio.CancelledError:
        pass
    assert calls == ["x", "x", "x"]
