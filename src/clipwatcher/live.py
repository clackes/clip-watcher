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

from clipwatcher import config

log = logging.getLogger("clipwatcher.live")


def data_root() -> Path:
    return Path(os.environ.get("CLIPWATCHER_DATA", config.ROOT / "work"))


def _recent_chat(login: str, exclude: str, max_age_s: float = 600) -> list:
    """Chat logged in the last baseline_min before this run (a restart), oldest first. Nothing when the
    newest line is older than max_age_s: the stream was off and its old chat says nothing about now."""
    import json

    from clipwatcher.models import ChatMessage

    base_s = float(config.global_config().get("detector", {}).get("baseline_min", 15)) * 60
    now_ms = int(time.time() * 1000)
    files = sorted((data_root() / "chat" / login.lower()).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    out: list = []
    for f in [f for f in files if f.stem != exclude][-2:]:
        try:
            for ln in f.read_text(encoding="utf-8").splitlines():
                d = json.loads(ln)
                if d["ts_ms"] >= now_ms - base_s * 1000:
                    out.append(ChatMessage(**d))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    out.sort(key=lambda m: m.ts_ms)
    if not out or out[-1].ts_ms < now_ms - max_age_s * 1000:
        return []
    return out


async def run_streamer(login: str, *, db, bot=None, staging_root: Path | None = None) -> None:
    from clipwatcher.chat.live_log import LiveLog, logged
    from clipwatcher.chat.twitch_irc import TwitchChat
    from clipwatcher.detect.live import LiveDetector
    from clipwatcher.fastlane import FastLane, Steps
    from clipwatcher.ingest.recorder import Recorder

    scfg = config.streamer(login)
    platform = scfg.get("platform", "twitch")
    if platform != "twitch":
        from clipwatcher.chat.kick import KickHistoryChat

        chat_src = KickHistoryChat(login)
    else:
        chat_src = TwitchChat(login)
    stream_id = time.strftime("%Y%m%d-%H%M%S")
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
        from clipwatcher.queue.telegram_bot import auto_publish, post_card

        steps.post_card = post_card
        steps.auto_publish = auto_publish
    from clipwatcher.ingest.recorder import sweep_stale

    for d in sweep_stale(data_root() / "ring", login, keep=rec.dir):
        log.info("%s: removed stale ring %s", login, d)
    await rec.start()
    log.info("%s: recording to %s", login, rec.dir)
    try:
        chat = logged(chat_src, LiveLog(login, stream_id, data_root() / "chat"))
        det = LiveDetector(login)
        n = det.prime(_recent_chat(login, exclude=stream_id))
        if n:
            log.info("%s: chat baseline primed with %d logged messages", login, n)
        lane = FastLane(login, chat, det, rec.ring, steps=steps, db=db, bot=bot,
                        staging_root=staging_root)  # fmt: skip
        await lane.run()
    finally:
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
