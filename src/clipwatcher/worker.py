"""Overnight / batch worker over ClipDB jobs (leases + attempts).

Stages: detect -> screen -> transcribe -> judge -> render -> stage. Each stage job carries a JSON payload and
enqueues the next stage on success. A crash leaves the lease to expire, so another run reclaims the job.
Outputs are idempotent: rendered files are written to a temp name and renamed to
clips/<stream>/<start_ms>-<end_ms>.mp4, and an existing output is reused.

CLI: python -m clipwatcher.worker --once [--db PATH] [--enqueue-media MEDIA --chat CHAT.jsonl --streamer S]
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import config
from .models import Candidate, JudgeResult, Word
from .queue import staging
from .queue.db import ClipDB

log = logging.getLogger(__name__)

STAGES = ("detect", "screen", "transcribe", "judge", "render", "stage")
Handler = Callable[[dict[str, Any], "Worker"], list[dict[str, Any]]]  # payload -> next-stage payloads


def clip_base(streamer: str, cand: dict[str, Any], root: Path) -> Path:
    return root / streamer / f"{int(cand['start_s'] * 1000)}-{int(cand['end_s'] * 1000)}"


def _cand(p: dict[str, Any]) -> Candidate:
    return Candidate(**p["cand"])


def _words(p: dict[str, Any]) -> list[Word]:
    return [Word(**w) for w in p.get("words", [])]


# --------------------------------------------------------------------------- default stage handlers
def _lines(msgs: list, c: Candidate) -> list[str]:
    """Chat of the window; when it must be cut, the end is kept: that is where the reaction is."""
    lo, hi = c.start_s * 1000, c.end_s * 1000
    return [f"{m.user}: {m.text}" for m in msgs if lo <= m.ts_ms <= hi][-2000:]


def h_detect(p: dict[str, Any], w: Worker) -> list[dict[str, Any]]:
    from .chat.convert import read_jsonl
    from .detect.detector import detect

    vod = config.global_config().get("vod", {})
    msgs = read_jsonl(p["chat"])
    cands = detect(
        msgs, streamer=p["streamer"], top=vod.get("top", 5), before_s=vod.get("before_s", 20),
        after_s=vod.get("after_s", 10), gap_s=vod.get("gap_s", 90), category=p.get("category"),
    )  # fmt: skip
    return [{**p, "cand": asdict(c), "chat_lines": _lines(msgs, c)} for c in cands]


def h_screen(p: dict[str, Any], w: Worker) -> list[dict[str, Any]]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return [p]
    from .judge import claude

    try:
        s = claude.screen("", p.get("chat_lines", []), p.get("title", ""), p.get("game", ""))
    except Exception as e:  # noqa: BLE001
        log.warning("screen failed (%s); passing through", e)
        return [p]
    if s.get("reject_reason") and max(s["funny_score"], s["hype_score"]) < 5:
        return []
    return [p]


def h_transcribe(p: dict[str, Any], w: Worker) -> list[dict[str, Any]]:
    from .judge import transcribe

    c = _cand(p)
    try:
        words = transcribe.transcribe(p["media"], c.start_s, c.end_s - c.start_s, p.get("language"))
    except Exception as e:  # noqa: BLE001
        log.warning("transcribe failed (%s); continuing without words", e)
        words = []
    return [{**p, "words": [asdict(x) for x in words]}]


def _frames_dir(p: dict[str, Any], c: Candidate, w: Worker) -> Path:
    return w.clips_root / "frames" / f"{p['streamer']}-{int(c.start_s * 1000)}"


def _frames_and_loud(p: dict[str, Any], c: Candidate, w: Worker, jcfg: dict[str, Any]) -> tuple[list, list]:
    """What the judge looks at besides the transcript, as in the live and VOD lanes (this lane sent it no
    frames at all). A failed read means judging without, not a failed job."""
    try:
        from .judge import frames as fr
        from .render import cold_open

        media, dur = Path(p["media"]), c.end_s - c.start_s
        size = jcfg.get("frame_size", [480, 270])
        frames = fr.keyframes_lowres(
            media, c.start_s, dur, n=fr.frame_count(dur), peak_rel=c.peak_s - c.start_s,
            size=(int(size[0]), int(size[1])), quality=int(jcfg.get("frame_quality", 65)),
            out_dir=_frames_dir(p, c, w),
        )  # fmt: skip
        return frames, cold_open.loud_moments(cold_open.loudness(media, c.start_s, dur))
    except Exception as e:  # noqa: BLE001
        log.warning("frames/loudness for the judge failed (%s); judging without", e)
        return [], []


def h_judge(p: dict[str, Any], w: Worker) -> list[dict[str, Any]]:
    from .judge import claude

    c, words = _cand(p), _words(p)
    jcfg = config.global_config().get("judge", {})
    if jcfg.get("mode", "single") == "off":
        return [{**p, "judge": asdict(claude.heuristic_judge(c, words))}]
    frames, loud = _frames_and_loud(p, c, w, jcfg)
    try:
        scfg = config.streamer(p["streamer"])
        jr: JudgeResult = claude.judge_clip(
            c, words, p.get("chat_lines", []), frames, game=scfg.get("game") or "",
            platform=scfg.get("platform"), language=p.get("language"), loud=loud,
        )  # fmt: skip
    except Exception as e:  # noqa: BLE001
        log.warning("judge failed (%s); heuristic fallback", e)
        jr = claude.heuristic_judge(c, words)
    finally:
        shutil.rmtree(_frames_dir(p, c, w), ignore_errors=True)
    if jr.source == "claude" and not jr.ok:  # rejected or flagged, like the live lane
        log.info("judge rejected %s at %.0fs: %s", p["streamer"], c.start_s, jr.reject_reason or jr.why)
        return []
    return [{**p, "judge": asdict(jr)}]


def h_render(p: dict[str, Any], w: Worker) -> list[dict[str, Any]]:
    from .render import cold_open
    from .render.template import render_clean, render_short

    c, scfg = _cand(p), config.streamer(p["streamer"])
    base = clip_base(p["streamer"], p["cand"], w.clips_root)
    short, clean = base.with_suffix(".mp4"), base.with_name(base.name + ".clean.mp4")
    start, dur = c.start_s, c.end_s - c.start_s
    j = p.get("judge") or {}
    words = _words(p)
    pl = cold_open.plan(Path(p["media"]), start, dur, c.peak_s - c.start_s, j, words)
    ts, te, co = pl.t0, pl.t1, pl.cold_open  # late start + cold open; captions re-timed
    words = [Word(w.start - ts, w.end - ts, w.text) for w in words if w.end > ts and w.start < te]
    start, dur = start + ts, te - ts
    hook = j.get("hook_text") or "Chat was NOT ready for this 💀"
    beats = cold_open.clip_beats(j, pl)
    actions = cold_open.clip_actions(j, pl)
    clip_id = f"{p['streamer']}-{int(c.start_s * 1000)}"
    if not short.exists():
        w.atomic(short, lambda tmp: render_short(
            Path(p["media"]), start, dur, tmp, clip_id=clip_id, streamer=p["streamer"], words=words,
            hook=hook, has_webcam=bool(scfg.get("has_webcam", True)), cam=scfg.get("facecam_rect"),
            game=scfg.get("gameplay_rect"), profile=w.profile, cold_open=co, beats=beats, actions=actions,
        ))  # fmt: skip
    if not clean.exists():
        w.atomic(clean, lambda tmp: render_clean(Path(p["media"]), start, dur, tmp, profile=w.profile))
    return [{**p, "short": str(short), "clean": str(clean), "clip_id": clip_id}]


def h_stage(p: dict[str, Any], w: Worker) -> list[dict[str, Any]]:
    from .queue.staging import stage_package

    c, j = _cand(p), p.get("judge") or {}
    meta = {
        "streamer": p["streamer"], "credit": config.credit(p["streamer"]), "lane": "batch",
        "hook": j.get("hook_text"), "titles": j.get("title_options", []), "setup_line": j.get("setup_line"),
        "title": (j.get("title_options") or [None])[0], "description": j.get("description"),
        "judge_source": j.get("source"), "start_s": c.start_s, "end_s": c.end_s,
        "score": j.get("score") if j.get("source") == "claude" else c.final_score,
    }  # fmt: skip
    if j.get("source") == "claude":
        meta.update(why=j.get("why"), flags=j.get("flags"), judge_score=j.get("score"))
    from .render.captions import caption_style

    meta["captions"] = caption_style(p["clip_id"])
    stage_package(p["clip_id"], Path(p["short"]), Path(p["clean"]), meta, root=w.staging_root, db=w.db)
    return []


DEFAULT_HANDLERS: dict[str, Handler] = {
    "detect": h_detect, "screen": h_screen, "transcribe": h_transcribe,
    "judge": h_judge, "render": h_render, "stage": h_stage,
}  # fmt: skip


class Worker:
    def __init__(
        self,
        db: ClipDB,
        *,
        handlers: dict[str, Handler] | None = None,
        clips_root: Path | None = None,
        staging_root: Path | None = None,
        lease_s: float = 600,
        max_attempts: int = 3,
        profile: str = "cpu_batch",
    ):
        self.db = db
        self.handlers = {**DEFAULT_HANDLERS, **(handlers or {})}
        self.clips_root = Path(clips_root) if clips_root else config.ROOT / "clips"
        self.staging_root = Path(staging_root) if staging_root else staging.staging_root()
        self.lease_s, self.max_attempts, self.profile = lease_s, max_attempts, profile

    @staticmethod
    def atomic(final: Path, produce: Callable[[Path], Any]) -> Path:
        """Render to a temp name next to final, then rename. A crash never leaves a half file at final."""
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(final.stem + ".part" + final.suffix)
        tmp.unlink(missing_ok=True)
        produce(tmp)
        os.replace(tmp, final)
        return final

    def enqueue(self, stage: str, payload: dict[str, Any]) -> int:
        return self.db.enqueue_job(stage, payload)

    def _requeue(self, job_id: int, err: str) -> None:
        self.db._conn().execute(
            "UPDATE jobs SET state='PENDING', lease_until=NULL, error=? WHERE id=?", (err[:500], job_id)
        )

    def step(self) -> bool:
        """Run one job (latest stage first so work drains). False when nothing is claimable."""
        for stage in reversed(STAGES):
            job = self.db.claim_job(stage, self.lease_s)
            if job is None:
                continue
            jid = job["id"]
            if job["attempts"] > self.max_attempts:  # reclaimed after too many crashes
                self.db.finish_job(jid, error=f"gave up after {job['attempts'] - 1} attempts")
                return True
            try:
                nexts = self.handlers[stage](job["payload"], self)
            except Exception as e:  # noqa: BLE001
                log.warning("job %s stage=%s attempt=%s failed: %s", jid, stage, job["attempts"], e)
                if job["attempts"] >= self.max_attempts:
                    self.db.finish_job(jid, error=f"{type(e).__name__}: {e}")
                else:
                    self._requeue(jid, f"{type(e).__name__}: {e}")
                return True
            nxt = STAGES[STAGES.index(stage) + 1] if stage != STAGES[-1] else None
            if nxt:
                for np_ in nexts:
                    self.db.enqueue_job(nxt, np_)
            self.db.finish_job(jid)
            return True
        return False

    def run_once(self) -> int:
        n = 0
        while self.step():
            n += 1
        return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipwatcher.worker")
    ap.add_argument("--once", action="store_true", help="drain the queue and exit")
    ap.add_argument("--db", type=Path, default=staging.staging_root() / "clips.db")
    ap.add_argument("--enqueue-media", type=Path)
    ap.add_argument("--chat", type=Path)
    ap.add_argument("--streamer")
    ap.add_argument("--language", default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    for name in ("httpx", "httpx2"):  # request URLs carry the Telegram bot token
        logging.getLogger(name).setLevel(logging.WARNING)
    a.db.parent.mkdir(parents=True, exist_ok=True)
    w = Worker(ClipDB(a.db))
    if a.enqueue_media:
        if not (a.chat and a.streamer):
            ap.error("--enqueue-media needs --chat and --streamer")
        w.enqueue("detect", {
            "media": str(a.enqueue_media), "chat": str(a.chat), "streamer": a.streamer.lower(),
            "language": a.language or config.stream_language(a.streamer),
        })  # fmt: skip
    if a.once:
        print(f"processed {w.run_once()} jobs")
        return 0
    import time

    while True:
        if not w.run_once():
            time.sleep(15)


if __name__ == "__main__":
    raise SystemExit(main())
