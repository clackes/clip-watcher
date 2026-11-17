"""Live fast lane: chat -> LiveDetector -> pin ring buffer -> cut -> transcribe -> screen -> render -> stage.

One FastLane per live streamer. Every external step is an injectable callable so the whole flow is testable
without ffmpeg, models or network. Target: about one minute from the end of the moment to the staged clip.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .models import Candidate, ChatMessage, JudgeResult, Word

log = logging.getLogger(__name__)

PIN_TTL_S = 48 * 3600


def fastlane_config() -> dict[str, Any]:
    c = config.global_config()
    base = {
        "max_parallel": 2,
        "post_roll_s": 5.0,
        "max_wait_s": 120.0,
        "profile": "cpu_live",
        "work_dir": str(config.ROOT / "work" / "fastlane"),
        "chat_context_s": 120.0,
        "min_score": 5.0,
    }
    base.update(c.get("fastlane", {}))
    base["pin_pad_s"] = c.get("buffer", {}).get("pin_pad_s", 60)
    base["segment_s"] = c.get("buffer", {}).get("segment_s", 10)
    return base


# --------------------------------------------------------------------------- default (lazy) steps
def _live_transcribe(media: Path, start_s: float, dur_s: float, language: str | None) -> list[Word]:
    """Small int8 model named by judge.live_whisper_model; no-op when faster-whisper is missing."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper not installed; live transcript skipped")
        return []
    import tempfile

    from .judge.transcribe import extract_wav, load_wav

    name = config.global_config().get("judge", {}).get("live_whisper_model", "small")
    model = _whisper(name, WhisperModel)
    with tempfile.TemporaryDirectory() as td:
        wav = extract_wav(media, start_s, dur_s, Path(td) / "w.wav")
        segs, _ = model.transcribe(load_wav(wav), word_timestamps=True, vad_filter=True, language=language)
        return [Word(w.start, w.end, w.word.strip()) for s in segs for w in (s.words or [])]


_MODELS: dict[str, Any] = {}


def _whisper(name: str, cls: Any) -> Any:
    if name not in _MODELS:
        _MODELS[name] = cls(name, compute_type="int8")
    return _MODELS[name]


def _default_screen(
    cand: Candidate, words: list[Word], chat_lines: list[str], title: str, game: str
) -> dict[str, Any]:
    """Cheap API screen when a key is present; otherwise pass-through scores from the detector."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        s = max(0.0, min(10.0, cand.final_score))
        return {"funny_score": s, "hype_score": s, "works_without_context": True, "reject_reason": None}
    from .judge import claude

    return claude.screen(" ".join(w.text for w in words), chat_lines, title, game)


def _default_heuristic(cand: Candidate, words: list[Word]) -> JudgeResult:
    from .judge import claude

    return claude.heuristic_judge(cand, words)


def _default_render_short(*a: Any, **k: Any) -> Path:
    from .render.template import render_short

    return render_short(*a, **k)


def _default_render_clean(*a: Any, **k: Any) -> Path:
    from .render.template import render_clean

    return render_clean(*a, **k)


def _default_stage(*a: Any, **k: Any) -> Path:
    from .queue.staging import stage_package

    return stage_package(*a, **k)


@dataclass
class Steps:
    transcribe: Callable[..., list[Word]] = _live_transcribe
    screen: Callable[..., dict[str, Any]] = _default_screen
    heuristic: Callable[..., JudgeResult] = _default_heuristic
    render_short: Callable[..., Path] = _default_render_short
    render_clean: Callable[..., Path] = _default_render_clean
    stage: Callable[..., Path] = _default_stage
    post_card: Callable[..., Any] | None = None  # async (app, db, clip_id, video)


@dataclass
class Outcome:
    clip_id: str
    status: str  # staged | rejected | failed
    timings: dict[str, float] = field(default_factory=dict)
    path: Path | None = None
    error: str | None = None


class FastLane:
    def __init__(
        self,
        streamer: str,
        chat: AsyncIterator[ChatMessage],
        detector: Any,
        ring: Any,
        *,
        steps: Steps | None = None,
        cfg: dict[str, Any] | None = None,
        db: Any = None,
        bot: Any = None,
        staging_root: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self.streamer = streamer
        self.chat = chat
        self.detector = detector
        self.ring = ring
        self.steps = steps or Steps()
        self.cfg = {**fastlane_config(), **(cfg or {})}
        self.db, self.bot, self.staging_root = db, bot, staging_root
        self.clock, self.sleep = clock, sleep
        self.recent: deque[ChatMessage] = deque()
        self.outcomes: list[Outcome] = []
        self._sem: asyncio.Semaphore | None = None
        self._tasks: set[asyncio.Task] = set()

    # -- main loop ----------------------------------------------------------
    async def run(self) -> list[Outcome]:
        self._sem = asyncio.Semaphore(int(self.cfg["max_parallel"]))
        async for msg in self.chat:
            self._remember(msg)
            cand = self.detector.feed(msg)
            if cand is not None and cand.chat_score < float(self.cfg["min_score"]):
                log.info("%s: skip weak moment score=%.1f", self.streamer, cand.chat_score)
                cand = None
            if cand is not None:
                t = asyncio.create_task(self._guarded(cand))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)
        if self._tasks:
            await asyncio.gather(*list(self._tasks))
        return self.outcomes

    def _remember(self, msg: ChatMessage) -> None:
        self.recent.append(msg)
        horizon = msg.ts_ms - int(float(self.cfg["chat_context_s"]) * 1000) * 3
        while self.recent and self.recent[0].ts_ms < horizon:
            self.recent.popleft()

    async def _guarded(self, cand: Candidate) -> None:
        assert self._sem is not None
        # pin immediately so the window cannot be pruned while we wait for a render slot
        self._pin(cand)
        async with self._sem:
            try:
                self.outcomes.append(await self.process(cand))
            except Exception as e:  # noqa: BLE001
                log.exception("fastlane candidate failed")
                self.outcomes.append(Outcome(self._clip_id(cand), "failed", error=str(e)))

    # -- helpers ------------------------------------------------------------
    def _clip_id(self, cand: Candidate) -> str:
        return f"{self.streamer}-live-{int(cand.start_s * 1000)}"

    def _pin(self, cand: Candidate) -> None:
        pad = float(self.cfg["pin_pad_s"])
        self.ring.pin(int((cand.start_s - pad) * 1000), int((cand.end_s + pad) * 1000), PIN_TTL_S)

    def _chat_lines(self, cand: Candidate) -> list[str]:
        lo, hi = int(cand.start_s * 1000), int(cand.end_s * 1000)
        return [f"{m.user}: {m.text}" for m in self.recent if lo <= m.ts_ms <= hi and not m.is_notice]

    async def _wait_for_buffer(self, need_end_s: float) -> None:
        """Wait until the ring buffer covers need_end_s. Uses ring.covered_until_ms() when present, else
        wall clock plus one segment."""
        deadline = self.clock() + float(self.cfg["max_wait_s"])
        covered = getattr(self.ring, "covered_until_ms", None)
        while self.clock() < deadline:
            if covered is not None:
                if covered() >= need_end_s * 1000:
                    return
            elif self.clock() >= need_end_s + float(self.cfg["segment_s"]):
                return
            await self.sleep(1.0)
        log.warning("buffer wait timed out for %s; cutting what exists", self.streamer)

    async def _timed(self, name: str, timings: dict[str, float], fn: Callable[..., Any], *a: Any, **k: Any):
        t0 = time.perf_counter()
        out = await asyncio.to_thread(fn, *a, **k)
        timings[name] = round(time.perf_counter() - t0, 2)
        log.info("fastlane %s step=%s %.2fs", self.streamer, name, timings[name])
        return out

    # -- one candidate ------------------------------------------------------
    async def process(self, cand: Candidate) -> Outcome:
        cid = self._clip_id(cand)
        scfg = config.streamer(self.streamer)
        timings: dict[str, float] = {}
        t_all = time.perf_counter()
        self._pin(cand)  # idempotent; also covers direct calls
        t0 = time.perf_counter()
        await self._wait_for_buffer(cand.end_s + float(self.cfg["post_roll_s"]))
        timings["wait"] = round(time.perf_counter() - t0, 2)

        work = Path(self.cfg["work_dir"]) / cid
        work.mkdir(parents=True, exist_ok=True)
        dur = cand.end_s - cand.start_s
        end_ms = int((cand.end_s + float(self.cfg["post_roll_s"])) * 1000)
        cut = await self._timed(
            "cut", timings, self.ring.cut, int(cand.start_s * 1000), end_ms, work / "src.ts"
        )
        # RingBuffer.cut returns (path, local_start_s): the clip starts that far into the concatenated
        # segments. Treating it as a bare path would start every clip at the segment boundary instead.
        src, t0_s = cut if isinstance(cut, tuple) else (cut, 0.0)

        words = await self._timed(
            "transcribe", timings, self.steps.transcribe, src, t0_s, dur, scfg.get("language")
        )
        chat_lines = self._chat_lines(cand)
        title, game = scfg.get("title", f"{self.streamer} live"), scfg.get("game", "")
        screen = await self._timed("screen", timings, self.steps.screen, cand, words, chat_lines, title, game)
        min_funny = config.global_config().get("judge", {}).get("min_funny", 5)
        if (
            screen.get("reject_reason")
            and max(screen.get("funny_score", 0), screen.get("hype_score", 0)) < min_funny
        ):
            self._unpin(cand)
            return Outcome(cid, "rejected", timings, error=str(screen["reject_reason"]))
        jr = self.steps.heuristic(cand, words)

        prof = self.cfg["profile"]
        from . import layout

        lay = await self._timed("layout", timings, layout.resolve, src, self.streamer)
        short = await self._timed(
            "render_short", timings, self.steps.render_short, src, t0_s, dur, work / "short_9x16.mp4",
            clip_id=cid, streamer=self.streamer, words=words, hook=jr.hook_text,
            **lay.as_render_kwargs(), profile=prof,
        )  # fmt: skip
        clean = await self._timed(
            "render_clean",
            timings,
            self.steps.render_clean,
            src,
            t0_s,
            dur,
            work / "clean_16x9.mp4",
            profile=prof,
        )
        meta = {
            "streamer": self.streamer, "credit": config.credit(self.streamer), "lane": "live",
            "campaign_link": scfg.get("campaign_link"), "hook": jr.hook_text, "titles": jr.title_options,
            "setup_line": jr.setup_line, "judge_source": jr.source, "score": cand.final_score,
            "chat_score": cand.chat_score, "final_score": cand.final_score, "peak_s": cand.peak_s,
            "start_s": cand.start_s, "end_s": cand.end_s, "funny_score": screen.get("funny_score"),
            "hype_score": screen.get("hype_score"), "layout": lay.source,
        }  # fmt: skip
        kw: dict[str, Any] = {"db": self.db}
        if self.staging_root:
            kw["root"] = self.staging_root
        pkg = await self._timed("stage", timings, self.steps.stage, cid, short, clean, meta, **kw)
        if self.bot is not None and self.db is not None and self.steps.post_card is not None:
            t1 = time.perf_counter()
            await self.steps.post_card(self.bot, self.db, cid, short)
            timings["post"] = round(time.perf_counter() - t1, 2)
        self._unpin(cand)
        timings["total"] = round(time.perf_counter() - t_all, 2)
        log.info("fastlane %s staged %s timings=%s", self.streamer, cid, timings)
        return Outcome(cid, "staged", timings, path=pkg)

    def _unpin(self, cand: Candidate) -> None:
        unpin = getattr(self.ring, "unpin", None)
        if unpin:
            pad = float(self.cfg["pin_pad_s"])
            unpin(int((cand.start_s - pad) * 1000), int((cand.end_s + pad) * 1000))
