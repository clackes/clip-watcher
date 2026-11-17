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
import time
from pathlib import Path

from clipwatcher import config

log = logging.getLogger("clipwatcher.live")


def data_root() -> Path:
    return Path(os.environ.get("CLIPWATCHER_DATA", config.ROOT / "work"))


async def run_streamer(login: str, *, db, bot=None, staging_root: Path | None = None) -> None:
    from clipwatcher.chat.live_log import LiveLog, logged
    from clipwatcher.chat.twitch_irc import TwitchChat
    from clipwatcher.detect.live import LiveDetector
    from clipwatcher.fastlane import FastLane, Steps
    from clipwatcher.ingest.recorder import Recorder

    scfg = config.streamer(login)
    platform = scfg.get("platform", "twitch")
    if platform != "twitch":
        from clipwatcher.chat.kick import KickChat

        chat_src = KickChat(login)
    else:
        chat_src = TwitchChat(login)
    stream_id = time.strftime("%Y%m%d-%H%M%S")
    buf = config.global_config().get("buffer", {})
    rec = Recorder(
        platform,
        login,
        stream_id,
        data_root() / "ring",
        quality=scfg.get("quality", "1080p60,720p60,720p,best"),
        segment_s=int(buf.get("segment_s", 10)),
        window_min=float(buf.get("window_min", 30)),
        min_free_disk_pct=float(buf.get("min_free_disk_pct", 15)),
    )
    steps = Steps()
    if bot is not None:
        from clipwatcher.queue.telegram_bot import post_card

        steps.post_card = post_card
    await rec.start()
    log.info("%s: recording to %s", login, rec.dir)
    try:
        chat = logged(chat_src, LiveLog(login, stream_id, data_root() / "chat"))
        lane = FastLane(login, chat, LiveDetector(login), rec.ring, steps=steps, db=db, bot=bot,
                        staging_root=staging_root)  # fmt: skip
        await lane.run()
    finally:
        await rec.stop()


async def supervise(login: str, **kw) -> None:
    delay = 5.0
    while True:
        t0 = time.time()
        try:
            await run_streamer(login, **kw)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: lane crashed; restarting", login)
        delay = 5.0 if time.time() - t0 > 300 else min(delay * 2, 300)
        await asyncio.sleep(delay)


async def main_async(logins: list[str], minutes: float) -> None:
    from clipwatcher.queue.db import ClipDB

    staging_root = Path(os.environ.get("CLIPWATCHER_STAGING", config.ROOT / "staging"))
    staging_root.mkdir(parents=True, exist_ok=True)
    db = ClipDB(staging_root / "clips.db")
    bot = None
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        from clipwatcher.queue.telegram_bot import build_application

        bot = build_application(db)
        await bot.initialize()
    tasks = [asyncio.create_task(supervise(lg, db=db, bot=bot, staging_root=staging_root)) for lg in logins]
    try:
        if minutes > 0:
            await asyncio.sleep(minutes * 60)
        else:
            await asyncio.gather(*tasks)
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
    logins = [
        s.strip().lower() for s in (a.streamers or ",".join(config.streamers())).split(",") if s.strip()
    ]
    if not logins:
        ap.error("no streamers")
    asyncio.run(main_async(logins, a.minutes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
