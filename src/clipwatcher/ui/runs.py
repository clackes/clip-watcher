"""Runs the companion app starts: one command at a time, the same a person would type, with everything it
says and does gathered as a numbered list of events for whoever is watching.

A run's folder (work/ui/runs/<id>/) keeps log.txt (what the command printed), events.jsonl (what the lane
reported through clipwatcher.events) and, once it has ended, run.json (how it went).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipwatcher import config

LOGIN = re.compile(r"[A-Za-z0-9_]{2,40}")
VOD = re.compile(r"[A-Za-z0-9:/._?=&%-]{4,200}")
RUN_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[a-z]{1,12}(-[0-9]{1,4})?")
_TEST = re.compile(r"^(tests/\S+?)::(\S+) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*(\d+)%\]")
_SUMMARY = re.compile(r"^=+ (.*?) in ([\d.]+)s")
_COUNT = re.compile(r"(\d+) (passed|failed|skipped|errors?|deselected|xfailed|xpassed)")
MAX_LOG_EVENTS = 4000
# what points the bot at this machine's own folders: a test run must not find them
TESTS_WITHOUT = (
    "CLIPWATCHER_EVENTS",
    "CLIPWATCHER_STAGING",
    "CLIPWATCHER_CONFIG",
    "CLIPWATCHER_LEDGER",
    "CLIPWATCHER_UI_WORK",
)


def runs_root() -> Path:
    env = os.environ.get("CLIPWATCHER_UI_WORK")
    return (Path(env).expanduser() if env else config.ROOT / "work" / "ui") / "runs"


def command(kind: str, params: dict[str, Any]) -> list[str]:
    """The command of a run. ValueError says, in words for the page, what is wrong with ``params``."""
    py = sys.executable
    if kind == "tests":
        cmd = [py, "-m", "pytest", "-v", "--color=no", "-p", "no:cacheprovider"]
        return [*cmd, "-m", "not ffmpeg"] if params.get("quick") else cmd
    if kind == "render":  # a staged clip's Short again, with another caption style or layout
        from clipwatcher.render.captions import CAPTION_STYLES
        from clipwatcher.ui.data import CLIP_ID

        clip_id = str(params.get("clip") or "")
        if not CLIP_ID.fullmatch(clip_id):
            raise ValueError("Pick a clip.")
        cmd = [py, "-m", "clipwatcher.ui.jobs", "render", clip_id]
        for flag, allowed in (("captions", CAPTION_STYLES), ("layout", ("split", "stack"))):
            value = params.get(flag)
            if value is None or value == "":
                continue
            if value not in allowed:
                raise ValueError(f"{flag}: one of {', '.join(allowed)}.")
            cmd += [f"--{flag}", str(value)]
        return cmd
    login = str(params.get("streamer") or "").lower()
    if not LOGIN.fullmatch(login):
        raise ValueError("Pick a streamer.")
    if kind == "live":
        minutes = float(params.get("minutes") or 0)
        if not 0 <= minutes <= 24 * 60:
            raise ValueError("Minutes: between 0 (until stopped) and 1440.")
        return [py, "-m", "clipwatcher.live", "--streamers", login, "--minutes", f"{minutes:g}"]
    if kind == "vod":
        platform, vod = str(params.get("platform") or "twitch"), str(params.get("vod") or "").strip()
        if platform not in ("twitch", "kick"):
            raise ValueError("Platform: twitch or kick.")
        if not VOD.fullmatch(vod):
            raise ValueError("Paste the link of the VOD, or its id.")
        top = int(params.get("top") or 3)
        if not 1 <= top <= 10:
            raise ValueError("Clips to take: between 1 and 10.")
        return [
            py,
            "-m",
            "clipwatcher.vod",
            "--platform",
            platform,
            "--vod",
            vod,
            "--streamer",
            login,
            "--top",
            str(top),
        ]
    raise ValueError(f"Unknown kind of run: {kind}")


class Run:
    def __init__(self, rid: str, kind: str, params: dict[str, Any], cmd: list[str], folder: Path):
        self.id, self.kind, self.params, self.cmd, self.dir = rid, kind, params, cmd, folder
        self.state = "running"  # running | done | failed | stopped
        self.started, self.ended, self.code = time.time(), None, None
        self.events: list[dict[str, Any]] = []
        self.cond = threading.Condition()
        self.proc: subprocess.Popen[str] | None = None
        self.stopping = False
        self.ring: str | None = None  # the live lane's recording, once it says where it is
        self.staged: list[str] = []
        self.tests: dict[str, Any] = {}
        self._logs = 0

    def add(self, kind: str, **data: Any) -> None:
        with self.cond:
            if kind == "log":
                self._logs += 1
                if self._logs > MAX_LOG_EVENTS:  # still in log.txt; the page has seen enough of it
                    return
            data.setdefault("t", round(time.time(), 3))
            self.events.append({"seq": len(self.events) + 1, "kind": kind, **data})
            self.cond.notify_all()

    def after(self, seq: int, timeout: float = 15.0) -> list[dict[str, Any]]:
        """Events numbered above ``seq``; waits up to ``timeout`` for the first one while the run goes on."""
        with self.cond:
            if len(self.events) <= seq and self.state == "running":
                self.cond.wait(timeout)
            return self.events[seq:]

    def summary(self) -> dict[str, Any]:
        end = self.ended or time.time()
        return {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "state": self.state,
            "started": self.started,
            "ended": self.ended,
            "seconds": round(end - self.started, 1),
            "code": self.code,
            "staged": list(self.staged),
            "tests": dict(self.tests),
            "ring": self.ring,
            "events": len(self.events),
        }


class RunManager:
    def __init__(self, root: Path | None = None, build: Callable[[str, dict[str, Any]], list[str]] = command):
        self.root = Path(root) if root else runs_root()
        self.build = build
        self.runs: dict[str, Run] = {}
        self.lock = threading.Lock()

    def active(self) -> Run | None:
        return next((r for r in self.runs.values() if r.state == "running"), None)

    def get(self, rid: str) -> Run | None:
        return self.runs.get(rid)

    def start(self, kind: str, params: dict[str, Any]) -> Run:
        with self.lock:
            busy = self.active()
            if busy is not None:
                raise RuntimeError(f"A {busy.kind} run is still going. Stop it or wait for it first.")
            cmd = self.build(kind, params)
            rid = base = time.strftime("%Y%m%d-%H%M%S") + f"-{kind}"
            n = 1
            while rid in self.runs or (self.root / rid).exists():  # two in the same second
                n += 1
                rid = f"{base}-{n}"
            folder = self.root / rid
            folder.mkdir(parents=True, exist_ok=True)
            run = Run(rid, kind, params, cmd, folder)
            self.runs[rid] = run
        run.add("start", run=rid, run_kind=kind, params=params, dry_run=config.dry_run())
        try:
            run.proc = self._spawn(run, cmd)
        except OSError as e:
            run.add("log", line=f"could not start: {e}")
            self._finish(run, None)
            return run
        threading.Thread(target=self._follow, args=(run,), daemon=True, name=f"run-{rid}").start()
        return run

    def _spawn(self, run: Run, cmd: list[str]) -> subprocess.Popen[str]:
        env = {**os.environ, "CLIPWATCHER_EVENTS": str(run.dir / "events.jsonl"), "PYTHONUNBUFFERED": "1"}
        if run.kind == "tests":
            # the tests see the project as it is checked out, never this machine's own folders
            for name in TESTS_WITHOUT:
                env.pop(name, None)
            env["DRY_RUN"] = "1"
        return subprocess.Popen(  # noqa: S603 - a fixed command list built by command(), never a shell string
            cmd,
            cwd=config.ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            errors="replace",
        )

    # -- following a run ---------------------------------------------------------------------------------
    def _follow(self, run: Run) -> None:
        assert run.proc is not None
        done = threading.Event()
        tail = threading.Thread(target=self._tail, args=(run, done), daemon=True)
        tail.start()
        self._read(run, run.proc)
        code = run.proc.wait()
        done.set()
        tail.join(timeout=5)
        self._finish(run, code)

    def _read(self, run: Run, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        with (run.dir / "log.txt").open("a", encoding="utf-8") as log:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                line = line.rstrip("\n")
                if run.kind == "tests":
                    self._test_line(run, line)
                if line.strip():
                    run.add("log", line=line[:500])

    def _test_line(self, run: Run, line: str) -> None:
        m = _TEST.match(line)
        if m:
            run.tests["seen"] = run.tests.get("seen", 0) + 1
            run.add("test", file=m.group(1), name=m.group(2), outcome=m.group(3), percent=int(m.group(4)))
            return
        m = _SUMMARY.match(line)
        if m and _COUNT.search(m.group(1)):
            counts = {
                k.rstrip("s") if k.startswith("error") else k: int(n) for n, k in _COUNT.findall(m.group(1))
            }
            run.tests.update(counts, seconds=float(m.group(2)))
            run.add("tests_done", **run.tests)

    def _tail(self, run: Run, done: threading.Event) -> None:
        """Pass on what the lane writes to its events file, as it writes it."""
        path, pos, rest = run.dir / "events.jsonl", 0, ""
        while True:
            finished = done.is_set()
            if path.exists():
                with path.open(encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                rest += chunk
                *lines, rest = rest.split("\n")
                for line in lines:
                    self._lane_event(run, line)
            if finished:
                return
            time.sleep(0.2)

    def _lane_event(self, run: Run, line: str) -> None:
        try:
            row = json.loads(line)
            kind = str(row.pop("kind"))
        except (ValueError, KeyError, AttributeError):
            return
        if kind == "recording":
            run.ring = row.get("ring")
        elif kind == "staged" and row.get("clip_id"):
            run.staged.append(str(row["clip_id"]))
        run.add(kind, **row)

    def _finish(self, run: Run, code: int | None) -> None:
        with run.cond:
            run.code, run.ended = code, time.time()
            run.state = "stopped" if run.stopping else "done" if code == 0 else "failed"
        run.add(
            "end",
            state=run.state,
            code=code,
            seconds=round(run.ended - run.started, 1),
            staged=run.staged,
            tests=run.tests,
        )
        try:
            (run.dir / "run.json").write_text(json.dumps(run.summary(), indent=2), encoding="utf-8")
            with (run.dir / "replay.jsonl").open("w", encoding="utf-8") as f:  # to watch the run again
                for e in list(run.events):
                    f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    # -- acting on a run ---------------------------------------------------------------------------------
    def stop(self, rid: str) -> bool:
        """Ask a run to end as Ctrl-C would (the live lane finishes the clip it is making), then insist."""
        run = self.runs.get(rid)
        if run is None or run.proc is None or run.state != "running":
            return False
        run.stopping = True
        run.add("stopping")
        proc = run.proc

        def press() -> None:
            for sig, wait in ((signal.SIGINT, 40.0), (signal.SIGTERM, 10.0), (signal.SIGKILL, 0.0)):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    return
                try:
                    proc.wait(timeout=wait or None)
                    return
                except subprocess.TimeoutExpired:
                    continue

        threading.Thread(target=press, daemon=True).start()
        return True

    def force(self, rid: str, seconds: float = 45.0) -> None:
        """Clip the last ``seconds`` of a live run now, through the same pipeline (scripts/force_clip.py)."""
        run = self.runs.get(rid)
        if run is None or run.kind != "live" or run.state != "running":
            raise RuntimeError("Only a live run that is going can be asked for a clip.")
        if not run.ring:
            raise RuntimeError("The recording has not started yet.")
        login = str(run.params.get("streamer") or "").lower()
        cmd = [
            sys.executable,
            str(config.ROOT / "scripts" / "force_clip.py"),
            run.ring,
            login,
            "--dur",
            f"{seconds:g}",
            "--secs-ago",
            f"{seconds:g}",
        ]
        proc = self._spawn(run, cmd)
        run.add("forced", seconds=seconds)

        def read() -> None:
            self._read(run, proc)
            proc.wait()

        threading.Thread(target=read, daemon=True).start()

    def events_of(self, rid: str) -> list[dict[str, Any]] | None:
        """Everything a run did, in order: from memory for a run of this session, else from its folder."""
        run = self.runs.get(rid)
        if run is not None:
            with run.cond:
                return list(run.events)
        if not RUN_ID.fullmatch(rid):
            return None
        try:
            lines = (self.root / rid / "replay.jsonl").read_text(encoding="utf-8").splitlines()
            return [json.loads(line) for line in lines if line.strip()]
        except (OSError, ValueError):
            return None

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Runs of this session and earlier ones (their run.json), newest first."""
        seen = {r.id: {**r.summary(), "replayable": True} for r in self.runs.values()}
        if self.root.is_dir():
            for d in self.root.iterdir():
                if d.name in seen or not (d / "run.json").is_file():
                    continue
                try:
                    row = json.loads((d / "run.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                # runs of before the events were kept can be listed, not watched again
                seen[d.name] = {**row, "replayable": (d / "replay.jsonl").is_file()}
        return sorted(seen.values(), key=lambda r: -float(r.get("started") or 0))[:limit]
