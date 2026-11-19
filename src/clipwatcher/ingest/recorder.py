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
import subprocess
import time
from pathlib import Path

from .. import config
from .hls_map import PdtMap
from .ringbuffer import RingBuffer

log = logging.getLogger(__name__)

QUALITY_DEFAULT = "720p60,720p,best"
# A run whose first segment ended more than this after spawn waited for the channel to go live.
ANCHOR_SLACK_MS = 30_000
_AD_START = re.compile(r"detected advertisement break(?: of (\d+) seconds?)?", re.I)
_AD_END = re.compile(r"resuming stream output", re.I)
_AD_ANY = re.compile(r"advertisement", re.I)


def channel_url(platform: str, login: str) -> str:
    return f"https://kick.com/{login}" if platform == "kick" else f"https://twitch.tv/{login}"


def build_streamlink_cmd(
    platform: str, login: str, quality: str = QUALITY_DEFAULT, binary: str | None = None
) -> list[str]:
    cmd = [
        binary or config.streamlink_bin(),
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
        self._anchor: dict[str, int] = {}  # run id -> wall-clock ms of the run's first video packet
        self._procs: list[asyncio.subprocess.Process] = []
        self._tasks: list[asyncio.Task] = []
        self._run_start_ms = 0
        self._run_id = ""
        self._n_runs = 0
        self.paused = False
        self.on_alert = on_alert  # async callable(text), e.g. a Telegram alert
        self.lag_alert_s = float(config.global_config().get("buffer", {}).get("lag_alert_s", 60))
        self._lagging = False
        self._last_ingest = time.time()  # wall clock of the last segment that arrived

    def newest_segment_age_s(self, now: float | None = None) -> float | None:
        rows = self.ring.segments()
        if not rows:
            return None
        return (time.time() if now is None else now) - rows[-1]["end_ms"] / 1000

    def ingest_run(self, run_id: str, run_start_ms: int) -> int:
        """Register finished segments from a run's csv into the ring; delete 0-byte files.

        Segment times in the csv count from the first video packet, not from when the pipeline was
        spawned. streamlink waits for an offline channel (--retry-streams), so a run spawned hours before
        the stream starts would stamp every segment hours early and every cut would miss the moment. The
        first finished segment's mtime (= its end, wall clock) re-anchors the run when the two disagree.
        """
        n = 0
        rows = read_segment_list(self.dir / f"{run_id}.csv")
        if rows and run_id not in self._anchor:
            name, _s, e = rows[0]
            f = self.dir / name
            anchor = run_start_ms
            if f.exists():
                from_file = int(f.stat().st_mtime * 1000 - e * 1000)
                if from_file - run_start_ms > ANCHOR_SLACK_MS:
                    log.info("%s: run %s started %.0fs after spawn", self.login, run_id,
                             (from_file - run_start_ms) / 1000)  # fmt: skip
                    anchor = from_file
            self._anchor[run_id] = anchor
        run_start_ms = self._anchor.get(run_id, run_start_ms)
        for name, s, e in rows:
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
        if n:
            self._last_ingest = time.time()
        return n

    def drop_unindexed(self) -> list[str]:
        """Delete segment files the index does not know. When a lane restarts into the same folder, the
        last segments of the previous run were written but never ingested, so pruning would never see them."""
        known = {r["name"] for r in self.ring.segments()}
        gone = [f.name for f in self.dir.glob("seg_*.ts") if f.name not in known]
        for name in gone:
            (self.dir / name).unlink(missing_ok=True)
        return gone

    async def start(self) -> None:
        await self.stop()
        self.drop_unindexed()
        await self._spawn()
        self._tasks.append(asyncio.create_task(self._ingest_loop()))

    async def _spawn(self) -> None:
        self._n_runs += 1
        self._run_id = f"r{int(time.time())}"
        self._run_start_ms = int(time.time() * 1000)
        # One shell pipeline in its own session = one process group holding streamlink and ffmpeg, so
        # stop() and the startup sweep can kill the whole recorder. (Separate processes can't share a
        # group across sessions on macOS, and killing only the Python parent left both recording.)
        sl_args = build_streamlink_cmd(self.platform, self.login, self.quality)
        ff_args = build_ffmpeg_cmd(self.dir, self._run_id, self.segment_s)
        if os.name == "nt":
            # cmd.exe pipes the two; its new process group is killed as a tree by taskkill (kill_group).
            line = f"{subprocess.list2cmdline(sl_args)} | {subprocess.list2cmdline(ff_args)} 2>NUL"
            sl = await asyncio.create_subprocess_shell(
                line,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            sl = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", f"{shlex.join(sl_args)} | {shlex.join(ff_args)} 2>/dev/null",
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
                    kill_group(self._procs[0].pid, _SIGKILL)
        self._procs = []
        (self.dir / "pids.json").unlink(missing_ok=True)

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        async for raw in proc.stderr:
            self.ads.feed(raw.decode(errors="replace"))

    async def _ingest_loop(self) -> None:
        """Runs for as long as the recorder does. One failed check (a full disk while rewriting the index,
        a file another thread holds open) must not end it: ffmpeg would keep writing segments that nobody
        ingests or prunes until the disk is full."""
        failing = False
        while True:
            await asyncio.sleep(self.segment_s)
            try:
                await self.check_once()
                failing = False
            except Exception as e:  # noqa: BLE001 - keep ingesting and pruning whatever went wrong once
                log.exception("%s: recorder check failed; retrying in %s s", self.login, self.segment_s)
                if not failing:  # one alert per streak of failures, not one every 10 s
                    failing = True
                    await self._alert(
                        f"{self.login}: recorder check failed ({type(e).__name__}: {e}); retrying"
                    )

    async def check_once(self) -> None:
        """Ingest finished segments, prune, and pause/resume recording on low disk."""
        if self._run_id:
            self.ingest_run(self._run_id, self._run_start_ms)
        alert = self.ring.prune().alert
        proc = self._procs[0] if self._procs else None
        if not alert and not self.paused and proc is not None and proc.returncode is not None:
            # streamlink waits for offline channels itself (--retry-streams), so an exit is an error
            # (bad binary, network, auth); respawn at most once a minute instead of silently stopping.
            if time.time() - self._run_start_ms / 1000 >= 60:
                log.warning("%s: recorder exited (code %s); restarting", self.login, proc.returncode)
                await self._kill_pipeline()
                await self._spawn()
        await self._check_lag()
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

    async def _check_lag(self) -> None:
        """Warn once when the recording falls behind live: the line cannot carry every stream at this
        quality, and clips would be cut from minutes-old video. Clears when it catches up."""
        lag = self.newest_segment_age_s()
        if lag is None or self.paused:
            return
        if time.time() - self._last_ingest > max(6 * self.segment_s, 60):
            # Nothing is arriving at all: the broadcast ended or the channel is offline. Behind live means
            # segments keep coming but old; a finished stream used to raise this alert every time.
            self._lagging = False
            return
        if lag > self.lag_alert_s and not self._lagging:
            self._lagging = True
            msg = (f"{self.login}: recording {lag / 60:.1f} min behind live - the connection cannot keep up; "
                   "record fewer streamers or lower buffer.quality")  # fmt: skip
            log.error(msg)
            await self._alert(msg)
        elif lag < self.lag_alert_s / 3 and self._lagging:
            self._lagging = False
            log.warning("%s: recording caught up with live", self.login)

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


_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)  # Windows has no SIGKILL


def kill_group(pgid: int, sig: int) -> None:
    if os.name == "nt":  # no process groups/signals: kill the whole tree under the cmd.exe pipeline
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pgid)], capture_output=True, check=False)
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def still_recorder(pid: int) -> bool:
    """Is this pid, written down by an earlier run, still its recorder pipeline? After a reboot the number
    may belong to any process, and the sweep kills what it finds with SIGKILL. Unknown (no ps, Windows)
    counts as yes: an orphaned recorder fills the disk."""
    if os.name == "nt":
        return True
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return True
    cmd = out.stdout.strip()
    return not cmd or "streamlink" in cmd  # gone already: killing the group is a no-op


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
                pgid = int(json.loads(pf.read_text())["pgid"])
                if still_recorder(pgid):
                    kill_group(pgid, _SIGKILL)
            except (ValueError, KeyError):
                pass
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d)
    return removed
