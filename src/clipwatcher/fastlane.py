"""Live fast lane: chat -> LiveDetector -> pin ring buffer -> cut -> transcribe -> screen -> render -> stage.

One FastLane per live streamer. Every external step is an injectable callable so the whole flow is testable
without ffmpeg, models or network. Target: about one minute from the end of the moment to the staged clip.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
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
        "min_score": 0.0,  # extra floor on the chat score; the real gate is the detector's (D13)
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
_MODELS_LOCK = threading.Lock()


def _whisper(name: str, cls: Any) -> Any:
    with _MODELS_LOCK:  # two clips transcribing at once must not both load the model
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


def _default_judge(
    cand: Candidate,
    words: list[Word],
    src: Path,
    t0_s: float,
    dur: float,
    chat_lines: list[str],
    game: str,
    language: str | None,
) -> JudgeResult | None:
    """Standardised multimodal judge (low-res frames + compact JSON). None = no key or judge.mode off,
    and the lane keeps its old screen + heuristic path."""
    jcfg = config.global_config().get("judge", {})
    mode = jcfg.get("mode", "single")
    if mode == "off" or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from .judge import claude
    from .judge import frames as fr

    if mode == "two_stage":
        s = claude.judge_clip(cand, words, chat_lines, [], game=game, language=language, task="screen")
        if s.source == "claude" and (not s.ok and s.score < int(jcfg.get("screen_min", 5))):
            return s
    size = tuple(jcfg.get("frame_size", [480, 270]))
    from .render import cold_open

    frames, loud = [], []
    try:  # a failed frame or audio read means judging from transcript and chat, not a failed clip
        loud = cold_open.loud_moments(cold_open.loudness(Path(src), t0_s, dur))
        frames = fr.keyframes_lowres(
            src, t0_s, dur, n=fr.frame_count(dur), peak_rel=cand.peak_s - cand.start_s,
            size=(int(size[0]), int(size[1])), quality=int(jcfg.get("frame_quality", 65)),
            out_dir=Path(src).parent / "frames",  # inside the clip's work dir, removed with it
        )  # fmt: skip
    except Exception as e:  # noqa: BLE001
        log.warning("%s: frames/loudness for the judge failed (%s); judging without", cand.stream, e)
    return claude.judge_clip(cand, words, chat_lines, frames, game=game, language=language, loud=loud)


def trim_words(words: list[Word], start: float, end: float) -> list[Word]:
    """Words inside [start, end], re-timed to the trimmed clip."""
    return [Word(w.start - start, w.end - start, w.text) for w in words if w.end > start and w.start < end]


def _default_heuristic(cand: Candidate, words: list[Word]) -> JudgeResult:
    from .judge import claude

    return claude.heuristic_judge(cand, words, game=config.streamer(cand.stream).get("game"))


def _default_loudness(src: Path, t0_s: float, dur: float) -> list[tuple[float, float]]:
    try:
        from .render import cold_open

        return cold_open.loudness(Path(src), t0_s, dur)
    except Exception as e:  # noqa: BLE001 - confirmation is a bonus: no audio read, no bonus
        log.warning("loudness read failed (%s)", e)
        return []


def _default_render_short(*a: Any, **k: Any) -> Path:
    from .render.template import render_short

    return render_short(*a, **k)


def _default_render_clean(*a: Any, **k: Any) -> Path:
    from .render.template import render_clean

    return render_clean(*a, **k)


def _default_stage(*a: Any, **k: Any) -> Path:
    from .queue.staging import stage_package

    return stage_package(*a, **k)


_SLOTS: dict[int, asyncio.Semaphore] = {}


def global_slots(n: int) -> asyncio.Semaphore:
    """One render semaphore shared by every streamer's lane in this event loop: nine streams live at once
    must not start eighteen whisper + ffmpeg jobs on one machine (fastlane.max_parallel_total)."""
    key = id(asyncio.get_running_loop())
    if key not in _SLOTS:
        _SLOTS[key] = asyncio.Semaphore(max(1, n))
    return _SLOTS[key]


@dataclass
class Steps:
    transcribe: Callable[..., list[Word]] = _live_transcribe
    screen: Callable[..., dict[str, Any]] = _default_screen
    heuristic: Callable[..., JudgeResult] = _default_heuristic
    render_short: Callable[..., Path] = _default_render_short
    render_clean: Callable[..., Path] = _default_render_clean
    stage: Callable[..., Path] = _default_stage
    post_card: Callable[..., Any] | None = None  # async (app, db, clip_id, video)
    judge: Callable[..., JudgeResult | None] = _default_judge  # None -> screen + heuristic path
    loudness: Callable[..., list[tuple[float, float]]] = _default_loudness  # [(t, dB)] of the cut window


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
        clips: Any = None,
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
        self._started: set[str] = set()  # clip ids already taken: the detector can fire twice on one window
        self.clips = clips  # viewer-made clips of this broadcast: anything with clip_confirms(moment) (D3)
        self._lines: dict[str, list[str]] = {}  # chat of a moment, taken when it fired
        self._last: tuple[float, float] | None = None  # (chat time, our clock) of the newest message

    # -- main loop ----------------------------------------------------------
    async def run(self) -> list[Outcome]:
        self._sem = asyncio.Semaphore(int(self.cfg["max_parallel"]))
        ticker = asyncio.create_task(self._tick()) if hasattr(self.detector, "tick") else None
        try:
            async for msg in self.chat:
                self._last = (msg.ts_ms / 1000, self.clock())
                self._remember(msg)
                self._start(self.detector.feed(msg))
            for cand in getattr(self.detector, "drain", list)():  # chat over: decide the spike in progress
                self._start(cand)
        except asyncio.CancelledError:
            for t in self._tasks:  # shutdown: do not hold it up for renders
                t.cancel()
            raise
        finally:
            if ticker is not None:
                ticker.cancel()
            # Chat ended or crashed: the clips already being made are finished first. The caller stops the
            # recorder right after this returns, and they still need its ring buffer.
            if self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
        return self.outcomes

    async def _tick(self) -> None:
        """A spike is decided when a later message moves the detector's clock, so in a chat that goes quiet
        right after it the clip waited for the next message. After 5 s of silence the clock is moved in chat
        time (last message + the silence, less a margin for late messages); after a minute it is left alone,
        so a dropped connection is not filled in as silence."""
        while True:
            await asyncio.sleep(1.0)
            if self._last is None:
                continue
            ts, at = self._last
            quiet = self.clock() - at
            if 5.0 < quiet <= 60.0:
                self._start(self.detector.tick(ts + quiet - 5.0))

    def _start(self, cand: Candidate | None) -> None:
        if cand is None:
            return
        if cand.chat_score < float(self.cfg["min_score"]):
            log.info("%s: skip weak moment score=%.1f", self.streamer, cand.chat_score)
            return
        if self._clip_id(cand) in self._started:
            log.info("%s: %s already in progress or done", self.streamer, self._clip_id(cand))
            return
        self._started.add(self._clip_id(cand))
        # now, while the chat of the moment is still in memory: a clip waiting for a render slot used to
        # reach the judge with no chat at all
        self._lines[self._clip_id(cand)] = self._chat_lines(cand)
        t = asyncio.create_task(self._guarded(cand))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _remember(self, msg: ChatMessage) -> None:
        self.recent.append(msg)
        horizon = msg.ts_ms - int(float(self.cfg["chat_context_s"]) * 1000) * 3
        while self.recent and self.recent[0].ts_ms < horizon:
            self.recent.popleft()

    async def _guarded(self, cand: Candidate) -> None:
        assert self._sem is not None
        # pin immediately so the window cannot be pruned while we wait for a render slot
        self._pin(cand)
        async with self._sem, global_slots(int(self.cfg.get("max_parallel_total", 2))):
            try:
                self.outcomes.append(await self.process(cand))
            except Exception as e:  # noqa: BLE001
                log.exception("fastlane candidate failed")
                self._unpin(cand)  # a failed clip must not hold its segments for 48 h
                self._cleanup(cand)
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

    async def _confirmed(
        self, cand: Candidate, src: Path, t0_s: float, dur: float, timings: dict[str, float]
    ) -> Candidate:
        """D3 confirmation: a loud instant where chat spiked, and a viewer who clipped the moment, raise the
        final score (and are shown to the operators). The chat score is left as the detector gave it."""
        from .detect.detector import confirm, loud_z

        series = await self._timed("loudness", timings, self.steps.loudness, src, t0_s, dur)
        clipped = self.clips is not None and self.clips.clip_confirms(cand.peak_s)
        return confirm(cand, [cand.peak_s] if clipped else (), loud_z(series, cand.peak_s - cand.start_s))

    async def _timed(self, name: str, timings: dict[str, float], fn: Callable[..., Any], *a: Any, **k: Any):
        t0 = time.perf_counter()
        out = await asyncio.to_thread(fn, *a, **k)
        timings[name] = round(time.perf_counter() - t0, 2)
        log.info("fastlane %s step=%s %.2fs", self.streamer, name, timings[name])
        return out

    # -- one candidate ------------------------------------------------------
    async def process(self, cand: Candidate) -> Outcome:
        cid = self._clip_id(cand)
        first_seg = getattr(self.ring, "segments", lambda: [])()
        if first_seg and cand.start_s * 1000 < first_seg[0]["start_ms"] - 2000:
            # The moment happened before this recording started (e.g. chat backlog at startup): cutting
            # would produce the wrong video, so skip it.
            log.info("%s: skip %s, before recording start", self.streamer, cid)
            self._unpin(cand)
            return Outcome(cid, "rejected", {}, error="before recording start")
        lang = config.stream_language(self.streamer)
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

        cand = await self._confirmed(cand, src, t0_s, dur, timings)
        words = await self._timed("transcribe", timings, self.steps.transcribe, src, t0_s, dur, lang)
        chat_lines = self._lines.pop(cid, None) or self._chat_lines(cand)
        title, game = scfg.get("title", f"{self.streamer} live"), scfg.get("game", "")
        jr = await self._timed(
            "judge", timings, self.steps.judge, cand, words, src, t0_s, dur, chat_lines, game, lang,
        )  # fmt: skip
        if jr is None:
            screen = await self._timed(
                "screen", timings, self.steps.screen, cand, words, chat_lines, title, game
            )
            min_funny = config.global_config().get("judge", {}).get("min_funny", 5)
            if (
                screen.get("reject_reason")
                and max(screen.get("funny_score", 0), screen.get("hype_score", 0)) < min_funny
            ):
                self._unpin(cand)
                self._cleanup(cand)
                return Outcome(cid, "rejected", timings, error=str(screen["reject_reason"]))
            jr = self.steps.heuristic(cand, words)
        else:
            screen = {"funny_score": jr.funny_score, "hype_score": jr.hype_score}
            if jr.source == "claude" and not jr.ok:
                log.info("%s: judge rejected %s (sc=%s): %s", self.streamer, cid, jr.score, jr.reject_reason)
                self._unpin(cand)
                self._cleanup(cand)
                return Outcome(cid, "rejected", timings, error=str(jr.reject_reason or jr.why))
        from .render import cold_open

        pl = cold_open.plan(src, t0_s, dur, cand.peak_s - cand.start_s, jr, words)
        beats = cold_open.clip_beats(jr, pl)
        actions = cold_open.clip_actions(jr, pl)
        peak_media = t0_s + (pl.reaction if pl.reaction is not None else cand.peak_s - cand.start_s)
        words = trim_words(words, pl.t0, pl.t1)
        t0_s, dur, co = t0_s + pl.t0, pl.t1 - pl.t0, pl.cold_open

        prof = self.cfg["profile"]
        from . import layout
        from .render.captions import caption_style

        lay = await self._timed(
            "layout",
            timings,
            layout.resolve,
            src,
            self.streamer,
            peak_s=peak_media,
            cam_hint=getattr(jr, "cam_xy", None),
        )
        short = await self._timed(
            "render_short", timings, self.steps.render_short, src, t0_s, dur, work / "short_9x16.mp4",
            clip_id=cid, streamer=self.streamer, words=words, hook=jr.hook_text,
            **lay.as_render_kwargs(), profile=prof, cold_open=co, beats=beats, actions=actions,
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
            "setup_line": jr.setup_line, "judge_source": jr.source,
            # queue score (auto_approve_min is on the judge's 1-10 scale); detector score otherwise
            "score": jr.score if jr.source == "claude" else cand.final_score,
            "chat_score": cand.chat_score, "final_score": cand.final_score, "peak_s": cand.peak_s,
            "start_s": cand.start_s, "end_s": cand.end_s, "funny_score": screen.get("funny_score"),
            "hype_score": screen.get("hype_score"), "layout": lay.source,
            "captions": caption_style(cid),
            # the spike's standing in its own stream: share of seconds that scored lower, rank so far
            "chat_pct": cand.signals.get("session_pct"), "chat_rank": cand.signals.get("session_rank"),
            "unique_chatters": cand.unique_chatters, "x_normal": round(cand.x_normal, 2),
            # D3 confirmation, already counted in final_score
            "loud_z": cand.signals.get("loud_z"), "viewer_clip": bool(cand.signals.get("viewer_clip")),
        }  # fmt: skip
        meta.update(title=jr.title_options[0], description=jr.description)  # heuristic copy too
        if jr.source == "claude":
            meta.update(why=jr.why, flags=jr.flags, judge_score=jr.score)
        kw: dict[str, Any] = {"db": self.db}
        if self.staging_root:
            kw["root"] = self.staging_root
        pkg = await self._timed("stage", timings, self.steps.stage, cid, short, clean, meta, **kw)
        if self.bot is not None and self.db is not None and self.steps.post_card is not None:
            t1 = time.perf_counter()
            try:
                await self.steps.post_card(self.bot, self.db, cid, short)
            except Exception as e:  # noqa: BLE001 - the clip is staged; the approval bot posts the card later
                log.warning(
                    "%s: card for %s not posted (%s); the approval bot will post it", self.streamer, cid, e
                )
            timings["post"] = round(time.perf_counter() - t1, 2)
        self._unpin(cand)
        self._cleanup(cand)  # staging holds copies; the cut .ts and renders would fill the disk 24/7
        timings["total"] = round(time.perf_counter() - t_all, 2)
        log.info("fastlane %s staged %s timings=%s", self.streamer, cid, timings)
        return Outcome(cid, "staged", timings, path=pkg)

    def _cleanup(self, cand: Candidate) -> None:
        if self.cfg.get("keep_work"):
            return
        shutil.rmtree(Path(self.cfg["work_dir"]) / self._clip_id(cand), ignore_errors=True)

    def _unpin(self, cand: Candidate) -> None:
        unpin = getattr(self.ring, "unpin", None)
        if unpin:
            pad = float(self.cfg["pin_pad_s"])
            unpin(int((cand.start_s - pad) * 1000), int((cand.end_s + pad) * 1000))
