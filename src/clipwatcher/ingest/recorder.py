"""Streamlink -> ffmpeg segment recorder (D1) with ad-break logging."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
import time
from pathlib import Path

from .. import config
from .hls_map import PdtMap
from .ringbuffer import RingBuffer

log = logging.getLogger(__name__)

QUALITY_DEFAULT = "720p60,720p,best"
_AD_START = re.compile(r"detected advertisement break(?: of (\d+) seconds?)?", re.I)
_AD_END = re.compile(r"resuming stream output", re.I)
_AD_ANY = re.compile(r"advertisement", re.I)


def channel_url(platform: str, login: str) -> str:
    return f"https://kick.com/{login}" if platform == "kick" else f"https://twitch.tv/{login}"


def build_streamlink_cmd(
    platform: str, login: str, quality: str = QUALITY_DEFAULT, binary: str = "streamlink"
) -> list[str]:
    cmd = [
        binary,
        "--retry-streams",
        "30",
        "--retry-max",
        "0",
        "--retry-open",
        "5",
        "--stream-segment-timeout",
        "60",
        "--hls-playlist-reload-attempts",
        "10",
    ]
    if platform != "kick":
        cmd += ["--twitch-supported-codecs", "h264"]
    cmd += ["--stdout", channel_url(platform, login), quality]
    return cmd


def build_ffmpeg_cmd(out_dir: Path, run_id: str, segment_s: int = 10, ffmpeg: str | None = None) -> list[str]:
    return [
        ffmpeg or config.ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        "pipe:0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(segment_s),
        "-reset_timestamps",
        "1",
        "-segment_format",
        "mpegts",
        "-segment_list",
        str(out_dir / f"{run_id}.csv"),
        "-segment_list_type",
        "csv",
        str(out_dir / f"seg_{run_id}_%05d.ts"),
    ]


def parse_ad_line(line: str) -> dict | None:
    """Parse an ad-related streamlink line into a start/end event dict, else None."""
    m = _AD_START.search(line)
    if m:
        return {"kind": "start", "duration_s": float(m.group(1)) if m.group(1) else None}
    if _AD_END.search(line):
        return {"kind": "end"}
    if _AD_ANY.search(line):
        return {"kind": "start", "duration_s": None}
    return None


class AdLog:
    """Turns ad start/end events into windows appended to ads.jsonl (epoch ms)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._open: int | None = None

    def feed(self, line: str, now_ms: int | None = None) -> dict | None:
        ev = parse_ad_line(line)
        if not ev:
            return None
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if ev["kind"] == "start":
            if self._open is not None:
                return None
            if ev["duration_s"]:
                return self._write(now_ms, now_ms + int(ev["duration_s"] * 1000))
            self._open = now_ms
            return None
        if self._open is None:
            return None
        s, self._open = self._open, None
        return self._write(s, now_ms)

    def _write(self, s: int, e: int) -> dict:
        row = {"start_ms": s, "end_ms": e}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row


def read_segment_list(csv_path: Path) -> list[tuple[str, float, float]]:
    """ffmpeg csv segment list: name,start,end (seconds from run start)."""
    rows = []
    if csv_path.exists():
        for ln in csv_path.read_text().splitlines():
            p = ln.strip().split(",")
            if len(p) >= 3:
                rows.append((p[0], float(p[1]), float(p[2])))
    return rows


class Recorder:
    """One streamer, one stream_id: ring dir = ring_root/<login>/<stream_id>."""

    def __init__(
        self,
        platform: str,
        login: str,
        stream_id: str,
        ring_root: str | Path,
        *,
        quality: str = QUALITY_DEFAULT,
        segment_s: int = 10,
        window_min: float = 30,
        min_free_disk_pct: float = 15,
        min_free_disk_gb: float = 5,
        on_alert=None,
    ):
        self.platform, self.login, self.stream_id = platform, login, stream_id
        self.quality, self.segment_s = quality, segment_s
        self.dir = Path(ring_root) / login / stream_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ring = RingBuffer(self.dir, window_min, segment_s, min_free_disk_pct, min_free_disk_gb)
        self.pdt = PdtMap(segment_s=segment_s)
        self.ads = AdLog(self.dir / "ads.jsonl")
        self._ingested: set[str] = set()
        self._procs: list[asyncio.subprocess.Process] = []
        self._tasks: list[asyncio.Task] = []
        self._run_start_ms = 0
        self._run_id = ""
        self._n_runs = 0
        self.paused = False
        self.on_alert = on_alert  # async callable(text), e.g. a Telegram alert

    def newest_segment_age_s(self, now: float | None = None) -> float | None:
        rows = self.ring.segments()
        if not rows:
            return None
        return (time.time() if now is None else now) - rows[-1]["end_ms"] / 1000

    def ingest_run(self, run_id: str, run_start_ms: int) -> int:
        """Register finished segments from a run's csv into the ring; delete 0-byte files."""
        n = 0
        for name, s, e in read_segment_list(self.dir / f"{run_id}.csv"):
            if name in self._ingested:
                continue
            f = self.dir / name
            if not f.exists() or f.stat().st_size == 0:
                f.unlink(missing_ok=True)
                self._ingested.add(name)
                continue
            start = int(run_start_ms + s * 1000)
            if self.ring.add_segment(f, start, e - s):
                self.pdt.add(int(round(s / self.segment_s)) + self._n_runs * 100_000, start)
                n += 1
            self._ingested.add(name)
        return n

    async def start(self) -> None:
        await self.stop()
        await self._spawn()
        self._tasks.append(asyncio.create_task(self._ingest_loop()))

    async def _spawn(self) -> None:
        self._n_runs += 1
        self._run_id = f"r{int(time.time())}"
        self._run_start_ms = int(time.time() * 1000)
        # One shell pipeline in its own session = one process group holding streamlink and ffmpeg, so
        # stop() and the startup sweep can kill the whole recorder. (Separate processes can't share a
        # group across sessions on macOS, and killing only the Python parent left both recording.)
        sl_cmd = shlex.join(build_streamlink_cmd(self.platform, self.login, self.quality))
        ff_cmd = shlex.join(build_ffmpeg_cmd(self.dir, self._run_id, self.segment_s))
        sl = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", f"{sl_cmd} | {ff_cmd} 2>/dev/null",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )  # fmt: skip
        (self.dir / "pids.json").write_text(json.dumps({"pgid": sl.pid}))
        self._procs = [sl]
        self._tasks.append(asyncio.create_task(self._read_stderr(sl)))

    async def _kill_pipeline(self) -> None:
        if self._procs:
            kill_group(self._procs[0].pid, signal.SIGTERM)
        for p in self._procs:
            if p.returncode is None:
                try:
                    await asyncio.wait_for(p.wait(), 10)
                except TimeoutError:
                    kill_group(self._procs[0].pid, signal.SIGKILL)
        self._procs = []
        (self.dir / "pids.json").unlink(missing_ok=True)

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        async for raw in proc.stderr:
            self.ads.feed(raw.decode(errors="replace"))

    async def _ingest_loop(self) -> None:
        while True:
            await asyncio.sleep(self.segment_s)
            await self.check_once()

    async def check_once(self) -> None:
        """Ingest finished segments, prune, and pause/resume recording on low disk."""
        if self._run_id:
            self.ingest_run(self._run_id, self._run_start_ms)
        alert = self.ring.prune().alert
        if alert and not self.paused:
            self.paused = True
            log.error("%s: %s", self.login, alert)
            await self._kill_pipeline()
            await self._alert(f"{self.login}: {alert}")
        elif not alert and self.paused:
            self.paused = False
            log.warning("%s: disk free again, recording resumed", self.login)
            await self._spawn()
            await self._alert(f"{self.login}: disk free again, recording resumed")

    async def _alert(self, text: str) -> None:
        if self.on_alert is None:
            return
        try:
            await self.on_alert(text)
        except Exception:  # noqa: BLE001 - an alert must never stop recording
            log.exception("alert failed")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        await self._kill_pipeline()


def kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def sweep_stale(ring_root: Path, login: str, keep: Path | None = None) -> list[Path]:
    """Kill recorders left by a previous run of this login and delete their ring folders.

    Called before a new recording starts; a previous process that died without cleanup (crash, kill -9,
    power loss) can otherwise keep recording into an orphaned folder until the disk is full.
    """
    removed = []
    base = Path(ring_root) / login
    for d in sorted(base.glob("*")) if base.exists() else []:
        if not d.is_dir() or (keep and d.resolve() == Path(keep).resolve()):
            continue
        pf = d / "pids.json"
        if pf.exists():
            try:
                kill_group(int(json.loads(pf.read_text())["pgid"]), signal.SIGKILL)
            except (ValueError, KeyError):
                pass
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d)
    return removed
