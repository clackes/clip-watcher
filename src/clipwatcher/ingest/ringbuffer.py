"""Segment ring buffer (D2): index.jsonl, pins, prune, cut."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from .disk import free_pct


@dataclass
class PruneResult:
    deleted: list[str] = field(default_factory=list)
    alert: str | None = None


class RingBuffer:
    def __init__(
        self,
        root: str | Path,
        window_min: float = 30,
        segment_s: float = 10,
        min_free_disk_pct: float = 15,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.window_ms = int(window_min * 60_000)
        self.segment_s = segment_s
        self.min_free = min_free_disk_pct
        self.index_path = self.root / "index.jsonl"
        self.pins_path = self.root / "pins.json"

    # -- index ---------------------------------------------------------
    def covered_until_ms(self) -> int:
        """Wall-clock end of the newest complete segment (0 when empty)."""
        return max((int(r["end_ms"]) for r in self.segments()), default=0)

    def segments(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        rows = []
        for ln in self.index_path.read_text().splitlines():
            if ln.strip():
                rows.append(json.loads(ln))
        return sorted(rows, key=lambda r: r["start_ms"])

    def _write_index(self, rows: list[dict]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        tmp.replace(self.index_path)

    def add_segment(self, path: str | Path, pdt_ms: int, duration_s: float | None = None) -> dict | None:
        p = Path(path)
        if not p.exists() or p.stat().st_size == 0:
            p.unlink(missing_ok=True)
            return None
        if p.parent.resolve() != self.root.resolve():
            p = Path(shutil.move(str(p), self.root / p.name))
        dur = duration_s if duration_s is not None else self.segment_s
        row = {"name": p.name, "start_ms": int(pdt_ms), "end_ms": int(pdt_ms + dur * 1000)}
        with self.index_path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    # -- pins ----------------------------------------------------------
    def _pins(self) -> list[dict]:
        if not self.pins_path.exists():
            return []
        return json.loads(self.pins_path.read_text())

    def pin(self, start_ms: int, end_ms: int, ttl_s: float = 48 * 3600, now: float | None = None) -> None:
        now = time.time() if now is None else now
        pins = self._pins()
        pins.append({"start_ms": int(start_ms), "end_ms": int(end_ms), "expires": now + ttl_s})
        self.pins_path.write_text(json.dumps(pins))

    def unpin(self, start_ms: int, end_ms: int) -> None:
        keep = [p for p in self._pins() if (p["start_ms"], p["end_ms"]) != (int(start_ms), int(end_ms))]
        self.pins_path.write_text(json.dumps(keep))

    # -- prune ---------------------------------------------------------
    def prune(self, now_ms: int | None = None, now: float | None = None) -> PruneResult:
        now = time.time() if now is None else now
        now_ms = int(now * 1000) if now_ms is None else now_ms
        res = PruneResult()
        pct = free_pct(self.root)
        if pct < self.min_free:
            res.alert = f"disk free {pct:.1f}% < {self.min_free}%: pruning stopped"
            return res
        pins = [p for p in self._pins() if p["expires"] > now]
        self.pins_path.write_text(json.dumps(pins))
        rows = self.segments()
        if not rows:
            return res
        newest = rows[-1]["name"]
        keep = []
        for r in rows:
            old = r["end_ms"] < now_ms - self.window_ms
            pinned = any(r["start_ms"] < p["end_ms"] and r["end_ms"] > p["start_ms"] for p in pins)
            if old and not pinned and r["name"] != newest:
                (self.root / r["name"]).unlink(missing_ok=True)
                res.deleted.append(r["name"])
            else:
                keep.append(r)
        if res.deleted:
            self._write_index(keep)
        return res

    # -- cut -----------------------------------------------------------
    def cut(self, start_ms: int, end_ms: int, out: str | Path) -> tuple[Path, float]:
        """Concat covering segments (stream copy) into `out` (.ts).

        Returns (path, local_start_s): offset of start_ms inside the output, for the D9 coarse seek + trim.
        """
        cover = [
            r
            for r in self.segments()
            if r["end_ms"] > start_ms and r["start_ms"] < end_ms and (self.root / r["name"]).exists()
        ]
        if not cover:
            raise LookupError("no segments cover the requested range")
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            for r in cover:
                f.write(f"file '{(self.root / r['name']).resolve()}'\n")
            lst = f.name
        try:
            cmd = [
                config.ffmpeg_bin(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                lst,
                "-c",
                "copy",
                "-f",
                "mpegts",
                str(out),
            ]
            subprocess.run(cmd, check=True, capture_output=True)
        finally:
            Path(lst).unlink(missing_ok=True)
        return out, max(0.0, (start_ms - cover[0]["start_ms"]) / 1000)
