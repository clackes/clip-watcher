"""One broadcast = one chat log on disk + one detector that has read all of it (D13).

``StreamSession`` sits where the fast lane expects a detector. For every broadcast of a streamer it
- keeps the whole chat in ``work/chat/<login>/<stream id>.jsonl``; nothing is pruned, unlike the video ring;
- on joining (bot start, restart, crash recovery) reads back what is saved and fetches the rest from the
  platform's chat replay, from the first second of the stream, so the spike baseline starts with history;
- starts a fresh log and detector when the streamer goes live again.

Chat that arrives while history is loading is held and fed afterwards, so nothing at the seam is lost and no
clip fires against a half-built baseline. Twitch's replay is read in seconds. Kick's is paced by Kick to about
35 messages a second, so there the newest stretch is read first (``chat.backfill_first_s``), detection starts
on that, and the rest of the stream is read in the background and the detector rebuilt on all of it.
Without a replay (VODs off on Twitch, the platform unreachable) the session starts from what is on disk and
the detector warms up on live chat as it always did.
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
from clipwatcher.chat.twitch_replay import Session
from clipwatcher.models import Candidate, ChatMessage

log = logging.getLogger(__name__)

# fetch(session, a_ms, b_ms, budget_s) -> (messages strictly between the two instants, instant reached).
# Reads backwards from b_ms; "instant reached" is a_ms when all was read, later when the budget ran out.
Fetcher = Callable[[Session, float, float, float | None], tuple[list[ChatMessage], int]]


def clip_poller(s: Session, login: str = "") -> Any:
    """Viewer-made clips of this broadcast (D3 confirmation), or None.

    Kick lists a channel's clips to anyone. Twitch's Helix needs an app registered with Twitch
    (TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET), which the live lane does not otherwise require."""
    if s.platform == "kick":
        from clipwatcher.detect.viewer_clips import KickClipPoller

        return KickClipPoller(login, s.stream_id)
    if not (s.user_id and os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET")):
        return None
    from clipwatcher.detect.viewer_clips import ViewerClipPoller

    return ViewerClipPoller(s.user_id, s.started_ms / 1000)


helix_clip_poller = clip_poller  # earlier name


def chat_config() -> dict[str, Any]:
    base = {
        "backfill": True,
        "backfill_max_h": 12.0,
        "backfill_first_s": 60.0,
        "rebuild_every_s": 120.0,
        "session_poll_s": 60.0,
    }
    base.update(config.global_config().get("chat", {}))
    return base


def _sources(platform: str) -> tuple[Any, Any]:
    """(broadcast lookup, history fetch) of a platform."""
    if platform == "twitch":
        return twitch_replay.live_session, twitch_replay.fetch_between
    if platform == "kick":
        from clipwatcher.chat import kick

        return kick.live_session, kick.fetch_between
    return None, None


def _key(m: ChatMessage) -> tuple[int, str, str]:
    return m.ts_ms, m.user, m.text


def _after(history: list[ChatMessage], later: list[ChatMessage]) -> list[ChatMessage]:
    """The messages of ``later`` that ``history`` does not hold yet (the two overlap by a few seconds)."""
    if not history:
        return list(later)
    last = history[-1].ts_ms
    recent = {_key(m) for m in history[-2000:] if m.ts_ms >= last - 5000}
    return [m for m in later if m.ts_ms > last - 5000 and _key(m) not in recent]


class StreamSession:
    def __init__(
        self,
        login: str,
        *,
        root: Path | None = None,
        platform: str = "twitch",
        make_detector: Callable[[str], Any] | None = None,
        lookup: Callable[[str], Session | None] | None = None,
        fetch: Fetcher | None = None,
        cfg: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
        make_poller: Callable[..., Any] = clip_poller,
    ):
        self.login, self.root, self.clock = login.lower(), root, clock
        self.cfg = {**chat_config(), **(cfg or {})}
        if make_detector is None:
            from clipwatcher.detect.live import LiveDetector

            make_detector = LiveDetector
        self._make = make_detector
        look, hist = _sources(platform)
        self._lookup = lookup if lookup is not None else look
        self._fetch: Fetcher | None = fetch if fetch is not None else hist
        self.detector: Any = None
        self.log: LiveLog | None = None
        self.stream_id: str | None = None  # the broadcast being followed; None before the first one
        self.primed = 0  # messages of history the current detector was loaded with
        self.rebuilds = 0  # times the detector was rebuilt on a deeper history
        self._loading = False
        self._held: list[ChatMessage] = []
        self._tail: list[ChatMessage] | None = None  # live chat taken while a rebuild is under way
        self._ready: deque[Candidate] = deque()
        self.on_rescue: Callable[[Candidate], None] | None = None  # the lane: clip a spike viewers confirmed
        self._make_poller = make_poller
        self._poller: Any = None
        self._poll_task: asyncio.Task | None = None
        self._deepen_task: asyncio.Task | None = None

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
        for task in (self._poll_task, self._deepen_task):
            if task is not None:
                task.cancel()
        if self.log is not None:
            self.log.close()

    # -- viewer clips (D3 confirmation) -------------------------------------------------------------
    def clip_windows(self) -> list[tuple[float, float]]:
        """Stretches of this broadcast viewers have clipped so far (epoch seconds); [] without a poller."""
        return [c.window for c in self._poller.seen.values()] if self._poller is not None else []

    def clip_confirms(self, moment_s: float) -> bool:
        """Did viewers clip this moment, and more than they clip any moment of this channel?"""
        from clipwatcher.detect.detector import clip_confirmed

        return clip_confirmed(self.clip_windows(), moment_s)

    def _on_clip(self, clip: Any) -> None:
        """A viewer just clipped something: a spike that fired there but stayed under the keep threshold is
        clipped after all, once the moment has drawn more clips than this channel's moments usually do."""
        det = self.detector
        for cand in list(getattr(det, "near", ())):
            if self.clip_confirms(cand.peak_s):
                for c in det.rescue(cand.peak_s, 1.0):
                    log.info("%s: viewer clips confirm the spike at %.0f", self.login, c.peak_s)
                    if self.on_rescue is not None:
                        self.on_rescue(c)

    def _watch_clips(self, s: Session) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
        try:
            self._poller = self._make_poller(s, self.login)
        except TypeError:  # a poller factory that takes the session only
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
        except Exception as e:  # noqa: BLE001 - the platform is unreachable: keep going with what we have
            log.warning("%s: broadcast lookup failed (%s)", self.login, e)
            found = None
        try:
            if found is not None and found.stream_id != self.stream_id:
                self._loading = True
                if self._deepen_task is not None:
                    self._deepen_task.cancel()
                new_log = LiveLog(self.login, found.stream_id, self.root)
                history, pending = await asyncio.to_thread(self._history, new_log, found)
                det = self._make(self.login)
                self.primed = await asyncio.to_thread(det.prime, history)
                self._open(found.stream_id, _after(history, self._held), new_log, det)
                self._watch_clips(found)
                log.info(
                    "%s: broadcast %s, baseline from %d messages of history (%.1f h), %d earlier spikes%s",
                    self.login, found.stream_id, self.primed,
                    (history[-1].ts_ms - history[0].ts_ms) / 3.6e6 if history else 0.0,
                    len(getattr(det, "spikes", [])),
                    "; reading the rest of the stream in the background" if pending else "",
                )  # fmt: skip
                if pending:
                    self._deepen_task = asyncio.create_task(self._deepen(found, pending))
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
        if self._tail is not None:
            self._tail.append(msg)
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
        self._tail = None
        self._flush(held)

    def _history(self, new_log: LiveLog, s: Session) -> tuple[list[ChatMessage], list[tuple[float, float]]]:
        """Saved chat of this broadcast plus, from the replay, what is missing before and after it. Also the
        stretches still to read: the newest hole comes first and gets ``backfill_first_s``; what does not
        fit (Kick's pace on a long busy stream) is left for the background."""
        saved = sorted(new_log.read(), key=lambda m: m.ts_ms)
        if not (self.cfg["backfill"] and s.vod_id and self._fetch):
            return saved, []
        now_ms = self.clock() * 1000
        lo_ms = max(s.started_ms, now_ms - float(self.cfg["backfill_max_h"]) * 3.6e6)
        if saved:
            holes = [(saved[-1].ts_ms, now_ms)]
            if saved[0].ts_ms - lo_ms > 120_000:
                holes.append((lo_ms, saved[0].ts_ms))
        else:
            holes = [(lo_ms, now_ms)]
        got: list[ChatMessage] = []
        pending: list[tuple[float, float]] = []
        left = float(self.cfg["backfill_first_s"])
        for a, b in holes:  # newest first
            if left <= 0:
                pending.append((a, b))
                continue
            t0 = time.monotonic()
            msgs, reached = self._fetch(s, a, b, left)
            left -= time.monotonic() - t0
            got += msgs
            if reached > a + 1000:
                pending.append((a, float(reached)))
                left = 0.0
        # Second look at the last half minute: the replay trails live by a few seconds and the fetch took
        # time, so the stretch right before the first live message is usually there by now.
        seam_ms = self._held[0].ts_ms if self._held else self.clock() * 1000
        seen = {_key(m) for m in got}
        got += [m for m in self._fetch(s, now_ms - 30_000, seam_ms, None)[0] if _key(m) not in seen]
        if not got:
            return saved, pending
        merged = sorted(saved + got, key=lambda m: m.ts_ms)
        new_log.rewrite(merged)
        return merged, pending

    # -- the rest of a long stream, in the background ------------------------------------------------
    async def _deepen(self, s: Session, pending: list[tuple[float, float]]) -> None:
        """Read the stretches the first pass left out, newest first, and rebuild the detector on the deeper
        history every ``rebuild_every_s`` and at the end. Never raises: live detection is not at stake."""
        assert self._fetch is not None
        older: list[ChatMessage] = []
        last = time.monotonic()
        try:
            for a, b in pending:
                while b > a + 1000 and self.stream_id == s.stream_id:
                    msgs, reached = await asyncio.to_thread(self._fetch, s, a, b, 30.0)
                    older += msgs
                    if reached >= b:  # no way further back
                        break
                    b = float(reached)
                    if time.monotonic() - last >= float(self.cfg["rebuild_every_s"]):
                        await self._rebuild(s, older, final=False)
                        last = time.monotonic()
            await self._rebuild(s, older, final=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("%s: reading the older chat failed; the baseline stays as it is", self.login)

    async def _rebuild(self, s: Session, older: list[ChatMessage], *, final: bool) -> None:
        """A new detector that has read ``older`` and everything in the log, swapped in between two
        messages. Live chat keeps going to the current detector until the swap; the new one takes over its
        cooldown and its near misses, so no moment is clipped twice and none is dropped."""
        if self.stream_id != s.stream_id or self.log is None or not older:
            return
        self._tail = []  # from here on live messages are also remembered: they are what the new one lacks
        saved = await asyncio.to_thread(lambda: sorted(self.log.read(), key=lambda m: m.ts_ms))
        merged = sorted(older + saved, key=lambda m: m.ts_ms)
        det = self._make(self.login)
        primed = await asyncio.to_thread(det.prime, merged)
        for _ in range(60):  # a spike being decided right now is decided by the detector that saw it start
            if self.stream_id != s.stream_id or not getattr(self.detector, "_maturing", None):
                break
            await asyncio.sleep(0.5)
        if self.stream_id != s.stream_id or self._loading:
            self._tail = None
            return
        # no await from here to the swap: nothing can arrive in between
        det.prime(_after(merged, self._tail))
        for attr in ("_last_keep", "_last_keep_score", "_kept", "near"):
            if hasattr(self.detector, attr) and hasattr(det, attr):
                setattr(det, attr, getattr(self.detector, attr))
        self.detector, self.primed, self._tail = det, primed, None
        self.rebuilds += 1
        if final:  # the log on disk gets the older chat too, in order
            self.log.rewrite(sorted(older + list(self.log.read()), key=lambda m: m.ts_ms))
        log.info(
            "%s: baseline now from %d messages (%.1f h of the stream)%s", self.login, primed,
            (merged[-1].ts_ms - merged[0].ts_ms) / 3.6e6, "" if final else ", still reading",
        )  # fmt: skip
