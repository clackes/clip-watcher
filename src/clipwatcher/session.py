"""One broadcast = one chat log on disk + one detector that has read all of it (D13).

``StreamSession`` sits where the fast lane expects a detector. For every broadcast of a streamer it
- keeps the whole chat in ``work/chat/<login>/<stream id>.jsonl``; nothing is pruned, unlike the video ring;
- on joining (bot start, restart, crash recovery) reads back what is saved and fetches the rest from Twitch's
  chat replay, from the first second of the stream, so the spike baseline starts with hours of history;
- starts a fresh log and detector when the streamer goes live again.

Chat that arrives while history is loading is held and fed afterwards, so nothing at the seam is lost and no
clip fires against a half-built baseline. Without a replay (VODs off, Kick, Twitch unreachable) the session
starts from what is on disk and the detector warms up on live chat as it always did.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipwatcher import config
from clipwatcher.chat import twitch_replay
from clipwatcher.chat.live_log import LiveLog
from clipwatcher.models import Candidate, ChatMessage

log = logging.getLogger(__name__)


def helix_clip_poller(s: twitch_replay.Session) -> Any:
    """Viewer-made clips of this broadcast (D3 confirmation), or None: Helix needs an app registered with
    Twitch (TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET), which the live lane does not otherwise require."""
    if not (s.user_id and os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET")):
        return None
    from clipwatcher.detect.viewer_clips import ViewerClipPoller

    return ViewerClipPoller(s.user_id, s.started_ms / 1000)


def chat_config() -> dict[str, Any]:
    base = {"backfill": True, "backfill_max_h": 12.0, "session_poll_s": 60.0}
    base.update(config.global_config().get("chat", {}))
    return base


class StreamSession:
    def __init__(
        self,
        login: str,
        *,
        root: Path | None = None,
        platform: str = "twitch",
        make_detector: Callable[[str], Any] | None = None,
        lookup: Callable[[str], twitch_replay.Session | None] | None = None,
        fetch: Callable[..., list[ChatMessage]] | None = None,
        cfg: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
        make_poller: Callable[[twitch_replay.Session], Any] = helix_clip_poller,
    ):
        self.login, self.root, self.clock = login.lower(), root, clock
        self.cfg = {**chat_config(), **(cfg or {})}
        if make_detector is None:
            from clipwatcher.detect.live import LiveDetector

            make_detector = LiveDetector
        self._make = make_detector
        twitch = platform == "twitch"
        self._lookup = lookup if lookup is not None else (twitch_replay.live_session if twitch else None)
        self._fetch = fetch if fetch is not None else (twitch_replay.fetch if twitch else None)
        self.detector: Any = None
        self.log: LiveLog | None = None
        self.stream_id: str | None = None  # the broadcast being followed; None before the first one
        self.primed = 0  # messages of history the current detector was loaded with
        self._loading = False
        self._held: list[ChatMessage] = []
        self._ready: deque[Candidate] = deque()
        self.on_rescue: Callable[[Candidate], None] | None = None  # the lane: clip a spike a viewer confirmed
        self._make_poller = make_poller
        self._poller: Any = None
        self._poll_task: asyncio.Task | None = None

    # -- the detector interface the fast lane uses ------------------------------------------------
    def feed(self, msg: ChatMessage) -> Candidate | None:
        if self._loading:
            self._held.append(msg)
        else:
            if self.detector is None:
                self._open(None, [])
            self._take(msg)
        return self._ready.popleft() if self._ready else None

    def tick(self, now_s: float) -> Candidate | None:
        if self.detector is not None and not self._loading:
            if (c := self.detector.tick(now_s)) is not None:
                self._ready.append(c)
        return self._ready.popleft() if self._ready else None

    def drain(self) -> list[Candidate]:
        out = list(self._ready) + (self.detector.drain() if self.detector is not None else [])
        self._ready.clear()
        return out

    def close(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
        if self.log is not None:
            self.log.close()

    # -- viewer clips (D3 confirmation) -------------------------------------------------------------
    def moment_times(self) -> list[float]:
        """Moments of this broadcast viewers have clipped so far (epoch seconds); [] without Helix access."""
        return self._poller.moment_times() if self._poller is not None else []

    def _on_clip(self, clip: Any) -> None:
        """A viewer just clipped something: a spike that fired there but stayed under the keep threshold is
        clipped after all."""
        rescue = getattr(self.detector, "rescue", None)
        for cand in rescue(clip.moment_s) if rescue else []:
            log.info("%s: viewer clip %s confirms the spike at %.0f", self.login, clip.id, cand.peak_s)
            if self.on_rescue is not None:
                self.on_rescue(cand)

    def _watch_clips(self, s: twitch_replay.Session) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
        self._poller = self._make_poller(s)
        self._poll_task = asyncio.create_task(self._poller.run(self._on_clip)) if self._poller else None

    # -- following the broadcast ---------------------------------------------------------------------
    async def watch(self) -> None:
        """Run next to the lane: find the current broadcast, load its history, notice the next one."""
        while True:
            await self.refresh()
            await asyncio.sleep(float(self.cfg["session_poll_s"]))

    async def refresh(self) -> None:
        """One look at what the streamer is broadcasting. Never raises: chat keeps flowing regardless."""
        first = self.detector is None
        if first:
            self._loading = True  # hold chat until we know which broadcast it belongs to
        try:
            found = await asyncio.to_thread(self._lookup, self.login) if self._lookup else None
        except Exception as e:  # noqa: BLE001 - Twitch unreachable: keep going with what we have
            log.warning("%s: broadcast lookup failed (%s)", self.login, e)
            found = None
        try:
            if found is not None and found.stream_id != self.stream_id:
                self._loading = True
                new_log = LiveLog(self.login, found.stream_id, self.root)
                history = await asyncio.to_thread(self._history, new_log, found)
                det = self._make(self.login)
                self.primed = await asyncio.to_thread(det.prime, history)
                last_ms = history[-1].ts_ms if history else 0
                self._open(found.stream_id, [m for m in self._held if m.ts_ms > last_ms], new_log, det)
                self._watch_clips(found)
                log.info(
                    "%s: broadcast %s, baseline from %d messages of history (%.1f h), %d earlier spikes",
                    self.login, found.stream_id, self.primed,
                    (history[-1].ts_ms - history[0].ts_ms) / 3.6e6 if history else 0.0,
                    len(getattr(det, "spikes", [])),
                )  # fmt: skip
            elif first:
                self._open(None, self._held)
        except Exception:  # noqa: BLE001 - history is a bonus; a failure must not stop live detection
            log.exception("%s: loading chat history failed; continuing on live chat only", self.login)
            if self.detector is None:
                self._open(None, self._held)
            else:
                self._flush(self._held)
        finally:
            self._loading = False
            self._held = []

    # -- internals -----------------------------------------------------------------------------------
    def _take(self, msg: ChatMessage) -> None:
        assert self.log is not None
        self.log.write(msg)
        if (c := self.detector.feed(msg)) is not None:
            self._ready.append(c)

    def _flush(self, held: list[ChatMessage]) -> None:
        for m in held:
            self._take(m)

    def _open(
        self, stream_id: str | None, held: list[ChatMessage], new_log: LiveLog | None = None, det: Any = None
    ) -> None:
        """Switch to a broadcast's log and detector (or, with no broadcast known, to a log named by start
        time and a cold detector), then feed the chat that was held meanwhile."""
        if self.log is not None:
            self.log.close()
        if new_log is None:
            new_log = LiveLog(self.login, time.strftime("offline-%Y%m%d-%H%M%S"), self.root)
        self.log, self.detector, self.stream_id = new_log, det or self._make(self.login), stream_id
        self._flush(held)

    def _history(self, new_log: LiveLog, s: twitch_replay.Session) -> list[ChatMessage]:
        """Saved chat of this broadcast plus, from the replay, whatever is missing before and after it."""
        saved = sorted(new_log.read(), key=lambda m: m.ts_ms)
        if not (self.cfg["backfill"] and s.vod_id and self._fetch):
            return saved
        now_ms = self.clock() * 1000
        lo_ms = max(s.started_ms, now_ms - float(self.cfg["backfill_max_h"]) * 3.6e6)
        if saved:
            holes = [(lo_ms, saved[0].ts_ms)] if saved[0].ts_ms - lo_ms > 120_000 else []
            holes.append((saved[-1].ts_ms, now_ms))
        else:
            holes = [(lo_ms, now_ms)]
        got: list[ChatMessage] = []
        for a, b in holes:
            got += self._replay(s, a, b)
        # Second look at the last half minute: the replay trails live by a few seconds and the fetch took
        # time, so the stretch right before the first live message is usually there by now.
        seam_ms = self._held[0].ts_ms if self._held else self.clock() * 1000
        seen = {(m.ts_ms, m.user, m.text) for m in got}
        got += [m for m in self._replay(s, now_ms - 30_000, seam_ms) if (m.ts_ms, m.user, m.text) not in seen]
        if not got:
            return saved
        merged = sorted(saved + got, key=lambda m: m.ts_ms)
        new_log.rewrite(merged)
        return merged

    def _replay(self, s: twitch_replay.Session, a_ms: float, b_ms: float) -> list[ChatMessage]:
        """Replayed messages strictly between two wall-clock times."""
        assert self._fetch is not None and s.vod_id
        off = lambda ms: (ms - s.started_ms) / 1000  # noqa: E731 - the VOD starts with the stream, give or take
        msgs = self._fetch(s.vod_id, off(a_ms) - 30, off(b_ms) + 30)
        return [m for m in msgs if a_ms < m.ts_ms < b_ms]
