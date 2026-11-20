"""Live fast lane entry point: record + anonymous chat + detect + clip, while the stream is still going.

    python -m clipwatcher.live --streamers kaicenat,clix [--minutes 0]

No Twitch login and no API keys are needed: streamlink waits for each channel to go live
(--retry-streams), chat is read anonymously over IRC TLS, and every detected moment is cut from the ring
buffer, captioned and staged (plus a Telegram card when TELEGRAM_BOT_TOKEN is set). Each streamer runs
in its own supervised task that restarts after any crash.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import signal
import time
from pathlib import Path

from clipwatcher import config, events

log = logging.getLogger("clipwatcher.live")


def data_root() -> Path:
    return Path(os.environ.get("CLIPWATCHER_DATA", config.ROOT / "work"))


GAME_POLL_S = 180.0  # while waiting: one streamlink metadata check per streamer every 3 min
GAME_RECHECK_S = 300.0  # while recording: has the streamer switched game?
OFFLINE_CHECKS = 3  # this many "no stream" checks in a row while recording: the stream ended


def _games() -> list[str]:
    return list(config.global_config().get("channel", {}).get("games") or [])


async def wait_for_game(platform: str, login: str, games: list[str], *, look=None, sleep=asyncio.sleep,
                        poll_s: float = GAME_POLL_S) -> str:  # fmt: skip
    """Block until the stream is live in one of the channel's games. Nothing is recorded meanwhile
    (owner, 19 Nov: GTA 6 streams only, never GTA 5 or anything else)."""
    from clipwatcher.ingest import category

    look = look or category.stream_category
    said: object = object()
    while True:
        cat = await asyncio.to_thread(look, platform, login)
        if cat and category.allowed(cat, games):
            return cat
        if cat != said:
            log.info("%s: not recording, %s", login, f"playing {cat!r}" if cat else "offline")
            said = cat
        await sleep(poll_s)


async def left_game(platform: str, login: str, games: list[str], *, look=None, sleep=asyncio.sleep,
                    poll_s: float = GAME_RECHECK_S) -> str:  # fmt: skip
    """Return once the stream switched to another category, or stayed offline for OFFLINE_CHECKS checks."""
    from clipwatcher.ingest import category

    look = look or category.stream_category
    gone = 0
    while True:
        await sleep(poll_s)
        cat = await asyncio.to_thread(look, platform, login)
        if cat is None:
            gone += 1
            if gone >= OFFLINE_CHECKS:
                return "offline"
            continue
        gone = 0
        if not category.allowed(cat, games):
            return cat


async def run_streamer(
    login: str, *, db, bot=None, staging_root: Path | None = None, ring_id: str | None = None
) -> None:
    from clipwatcher.chat.twitch_irc import TwitchChat
    from clipwatcher.fastlane import FastLane, Steps
    from clipwatcher.ingest.recorder import Recorder
    from clipwatcher.session import StreamSession

    scfg = config.streamer(login)
    platform = scfg.get("platform", "twitch")
    games = _games()
    if games:
        game = await wait_for_game(platform, login, games)
        log.info("%s: live in %s, recording", login, game)
    if platform != "twitch":
        from clipwatcher.chat.kick import KickChat

        chat_src = KickChat(login)  # realtime websocket; reads the history after a reconnect
    else:
        chat_src = TwitchChat(login)
    # The ring folder outlives a lane restart (supervise passes the same id again): clips still being cut
    # from it keep their pinned video. Only folders of earlier processes are swept below.
    stream_id = ring_id or time.strftime("%Y%m%d-%H%M%S")
    buf = config.global_config().get("buffer", {})
    rec = Recorder(
        platform,
        login,
        stream_id,
        data_root() / "ring",
        quality=scfg.get("quality", buf.get("quality", "720p,720p30,720p60,best")),
        segment_s=int(buf.get("segment_s", 10)),
        window_min=float(buf.get("window_min", 30)),
        min_free_disk_pct=float(buf.get("min_free_disk_pct", 15)),
        min_free_disk_gb=float(buf.get("min_free_disk_gb", 5)),
        on_alert=_alerter(bot),
    )
    steps = Steps()
    if bot is not None:
        from clipwatcher.queue.telegram_bot import post_card

        steps.post_card = post_card
    from clipwatcher.ingest.recorder import sweep_stale

    for d in sweep_stale(data_root() / "ring", login, keep=rec.dir):
        log.info("%s: removed stale ring %s", login, d)
    await rec.start()
    log.info("%s: recording to %s", login, rec.dir)
    events.emit(
        "recording", streamer=login, ring=str(rec.dir), platform=config.streamer(login).get("platform")
    )
    # The chat of a broadcast is kept whole on disk and read back (plus Twitch's replay of what was said
    # before the bot joined) to build the spike baseline; the video above stays a rolling window.
    session = StreamSession(login, root=data_root() / "chat", platform=platform)
    watch = asyncio.create_task(session.watch())
    try:
        lane = FastLane(login, chat_src, session, rec.ring, steps=steps, db=db, bot=bot,
                        staging_root=staging_root, clips=session)  # fmt: skip
        session.on_rescue = lane._start  # a spike under the gate that a viewer then clipped
        run = asyncio.create_task(lane.run())
        if not games:
            await run
            return
        gone = asyncio.create_task(left_game(platform, login, games))
        done, _ = await asyncio.wait({run, gone}, return_when=asyncio.FIRST_COMPLETED)
        if gone in done:
            # Moments already being rendered finish from the ring files on disk.
            log.info("%s: now %s, recording stopped", login, gone.result())
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        else:
            gone.cancel()
            run.result()
    finally:
        watch.cancel()
        session.close()
        await rec.stop()


def _alerter(bot):
    if bot is None:
        return None
    from clipwatcher.queue.telegram_bot import alert

    async def send(text: str) -> None:
        await alert(bot, text)

    return send


async def supervise(login: str, **kw) -> None:
    delay = 5.0
    ring_id = time.strftime("%Y%m%d-%H%M%S")
    while True:
        t0 = time.time()
        try:
            await run_streamer(login, ring_id=ring_id, **kw)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: lane crashed; restarting", login)
        delay = 5.0 if time.time() - t0 > 300 else min(delay * 2, 300)
        await asyncio.sleep(delay)


async def main_async(logins: list[str], minutes: float) -> None:
    from clipwatcher.queue.db import ClipDB
    from clipwatcher.queue.staging import staging_root as shared_root

    staging_root = shared_root()  # the same folder the approval bot and the publishers use
    staging_root.mkdir(parents=True, exist_ok=True)
    db = ClipDB(staging_root / "clips.db")
    bot = None
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        from clipwatcher.queue.telegram_bot import build_application

        bot = build_application(db)
        await bot.initialize()
    tasks = [asyncio.create_task(supervise(lg, db=db, bot=bot, staging_root=staging_root)) for lg in logins]
    # SIGTERM (docker stop, launchd, kill) and Ctrl-C cancel the lanes so every recorder's finally-block
    # stops its streamlink/ffmpeg group instead of leaving them recording.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl-C raises KeyboardInterrupt, tasks are cancelled below
            pass
    try:
        waits = [asyncio.create_task(stop.wait())]
        if minutes > 0:
            waits.append(asyncio.create_task(asyncio.sleep(minutes * 60)))
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for w in waits:
            w.cancel()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipwatcher.live")
    ap.add_argument("--streamers", help="comma-separated logins (default: all in streamers.yaml)")
    ap.add_argument("--minutes", type=float, default=0, help="stop after N minutes (0 = run forever)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Telegram's URLs carry the bot token.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # commas or spaces (PowerShell turns "a,b" into the array "a b")
    logins = [s.lower() for s in re.split(r"[,\s]+", a.streamers or ",".join(config.streamers())) if s]
    skipped = [lg for lg in logins if not config.allowed_streamer(lg)]
    for lg in skipped:
        log.warning("%s: skipped, speaks %r but the channel is %r only", lg, config.stream_language(lg),
                    config.channel_language())  # fmt: skip
    logins = [lg for lg in logins if lg not in skipped]
    if not logins:
        ap.error("no streamers")
    asyncio.run(main_async(logins, a.minutes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
