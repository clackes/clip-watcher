#!/usr/bin/env python3
"""Record a short sample of live channels exactly as the live lane records them (webcam checks).

streamlink -> ffmpeg 10 s .ts segments (ingest/recorder.py commands), then the segments are joined by
stream copy like RingBuffer.cut. Segments that overlap an advertisement break are left out: a sample that
is an ad is not a sample.

.venv/bin/python scripts/webcam_record.py OUT_DIR login [kick:login ...] [--secs 80] [--quality best]
                                          [--parallel 3]
Writes OUT_DIR/<login>.ts and OUT_DIR/<login>.json (quality opened, qualities offered, ad breaks).
Kick channels are recorded one after another (D15: its request limit is shared with the bot).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from clipwatcher import config
from clipwatcher.ingest import recorder

_OPEN = re.compile(r"Opening stream: (\S+)")
_AVAIL = re.compile(r"Available streams: (.+)")
_KICK_LOCK = threading.Lock()


def record(out_dir: Path, target: str, secs: int, quality: str, wait_s: int = 150) -> dict:
    platform, _, login = target.rpartition(":")
    platform = platform or "twitch"
    info: dict = {"login": login, "platform": platform, "asked": quality, "ok": False}
    work = Path(tempfile.mkdtemp(prefix=f"rec_{login}_", dir=out_dir))
    log_path = out_dir / f"{login}.streamlink.log"
    sl_cmd = recorder.build_streamlink_cmd(platform, login, quality)
    sl_cmd.insert(1, "--loglevel=debug")
    ff_cmd = recorder.build_ffmpeg_cmd(work, "s", 10)
    t0 = time.time()
    with log_path.open("w") as lf:
        sl = subprocess.Popen(sl_cmd, stdout=subprocess.PIPE, stderr=lf)
        ff = subprocess.Popen(ff_cmd, stdin=sl.stdout, stderr=subprocess.DEVNULL)
        sl.stdout.close()  # type: ignore[union-attr]
        need = secs // 10 + 1
        first = None
        try:
            while True:
                time.sleep(2)
                rows = recorder.read_segment_list(work / "s.csv")
                if rows and first is None:
                    first = time.time()
                if len(rows) >= need:
                    break
                if sl.poll() is not None or ff.poll() is not None:
                    break
                if first is None and time.time() - t0 > wait_s:
                    info["error"] = f"no picture after {wait_s} s"
                    break
        finally:
            for p in (sl, ff):
                p.terminate()
            for p in (sl, ff):
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
    text = log_path.read_text(errors="replace")
    info["opened"] = (_OPEN.search(text) or [None, None])[1]
    avail = _AVAIL.search(text)
    info["offered"] = avail.group(1).strip() if avail else None
    ads = [ln.strip() for ln in text.splitlines() if recorder.parse_ad_line(ln)]
    info["ads"] = ads[:6]
    rows = recorder.read_segment_list(work / "s.csv")
    keep = [work / name for name, _s, _e in rows if (work / name).exists() and (work / name).stat().st_size]
    if len(keep) < 4:
        info.setdefault("error", f"only {len(keep)} segments")
        shutil.rmtree(work, ignore_errors=True)
        return info
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in keep))
    out = out_dir / f"{login}.ts"
    subprocess.run(
        [config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", "-f", "mpegts", str(out)],
        check=True, capture_output=True,
    )  # fmt: skip
    shutil.rmtree(work, ignore_errors=True)
    pr = subprocess.run(
        [config.ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate,codec_name,bit_rate:format=duration,bit_rate", "-of", "json",
         str(out)],
        capture_output=True, text=True,
    )  # fmt: skip
    meta = json.loads(pr.stdout or "{}")
    st = (meta.get("streams") or [{}])[0]
    info.update(
        ok=True, width=st.get("width"), height=st.get("height"), fps=st.get("r_frame_rate"),
        codec=st.get("codec_name"), duration=float(meta.get("format", {}).get("duration", 0)),
        kbps=round(float(meta.get("format", {}).get("bit_rate", 0)) / 1000),
    )  # fmt: skip
    return info


def run(out_dir: Path, target: str, secs: int, quality: str) -> dict:
    try:
        if target.startswith("kick:"):
            with _KICK_LOCK:  # one Kick channel at a time
                info = record(out_dir, target, secs, quality)
                time.sleep(2)
        else:
            info = record(out_dir, target, secs, quality)
    except Exception as e:  # noqa: BLE001
        info = {"login": target, "ok": False, "error": repr(e)}
    (out_dir / f"{info['login'].split(':')[-1]}.json").write_text(json.dumps(info, indent=1))
    print(json.dumps(info), flush=True)
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--secs", type=int, default=80)
    ap.add_argument("--quality", default="best")
    ap.add_argument("--parallel", type=int, default=3)
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(a.parallel) as ex:
        res = list(ex.map(lambda t: run(a.out_dir, t, a.secs, a.quality), a.targets))
    print(f"{sum(r.get('ok', False) for r in res)}/{len(res)} recorded", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
