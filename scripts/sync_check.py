"""Measure the real chat delay: cross-correlate streamer loudness (live ring buffer) with chat bursts.

    python scripts/sync_check.py [login ...]        # default: the 5 busiest chats with a ring

For each stream: audio RMS per 0.5 s (wall clock from the ring's segment times) and human chat messages per
0.5 s (tmi-sent-ts). Both series are turned into onsets (positive jumps) and correlated over lags 0-30 s.
The best lag is how long chat trails what is on our recording; the detector's delay_s should be near it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

from clipwatcher import config
from clipwatcher.chat.convert import read_jsonl
from clipwatcher.ingest.ringbuffer import RingBuffer

ROOT = config.ROOT / "work"
STEP = 0.5
SR = 8000


def latest(d: Path) -> Path | None:
    subs = sorted(p for p in d.iterdir()) if d.exists() else []
    return subs[-1] if subs else None


def audio_series(ring: RingBuffer) -> tuple[float, np.ndarray]:
    segs = ring.segments()
    t0 = segs[0]["start_ms"] / 1000
    end = segs[-1]["end_ms"] / 1000
    out = np.full(int((end - t0) / STEP) + 1, np.nan)
    for s in segs:
        pcm = subprocess.run(
            [config.ffmpeg_bin(), "-v", "error", "-i", str(ring.root / s["name"]), "-vn", "-ac", "1",
             "-ar", str(SR), "-f", "s16le", "-"], capture_output=True,
        ).stdout  # fmt: skip
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        n = int(SR * STEP)
        for i in range(len(a) // n):
            rms = float(np.sqrt(np.mean(a[i * n : (i + 1) * n] ** 2)) + 1e-3)
            k = int((s["start_ms"] / 1000 + i * STEP - t0) / STEP)
            if 0 <= k < len(out):
                out[k] = 20 * np.log10(rms)
    return t0, out


def chat_series(login: str, t0: float, n: int) -> np.ndarray:
    files = sorted((ROOT / "chat" / login).glob("*.jsonl"))
    out = np.zeros(n)
    for f in files[-2:]:
        for m in read_jsonl(f):
            if m.is_notice or m.is_bot:
                continue
            k = int((m.ts_ms / 1000 - t0) / STEP)
            if 0 <= k < n:
                out[k] += 1
    return out


def onsets(x: np.ndarray, smooth: int = 2) -> np.ndarray:
    x = np.where(np.isnan(x), np.nanmedian(x), x)
    if smooth > 1:
        x = np.convolve(x, np.ones(smooth) / smooth, mode="same")
    d = np.diff(x, prepend=x[0])
    d = np.clip(d, 0, None)
    return (d - d.mean()) / (d.std() + 1e-9)


def best_lag(audio: np.ndarray, chat: np.ndarray, max_s: float = 30) -> tuple[float, float, list]:
    a, c = onsets(audio), onsets(chat, smooth=4)
    res = []
    for k in range(0, int(max_s / STEP) + 1):
        n = len(a) - k
        if n < 120:
            break
        res.append((k * STEP, float(np.corrcoef(a[:n], c[k : k + n])[0, 1])))
    lag, r = max(res, key=lambda x: x[1])
    return lag, r, res


def main() -> None:
    logins = sys.argv[1:]
    if not logins:
        sizes = []
        for d in (ROOT / "chat").iterdir():
            f = sorted(d.glob("*.jsonl"))
            ring = latest(ROOT / "ring" / d.name)
            if f and ring and list(ring.glob("seg_*.ts")):
                sizes.append((f[-1].stat().st_size, d.name))
        logins = [n for _, n in sorted(sizes, reverse=True)[:5]]
    for login in logins:
        rd = latest(ROOT / "ring" / login)
        ring = RingBuffer(rd)
        if len(ring.segments()) < 30:
            print(f"{login}: only {len(ring.segments())} segments, skip")
            continue
        t0, audio = audio_series(ring)
        chat = chat_series(login, t0, len(audio))
        lag, r, res = best_lag(audio, chat)
        top3 = sorted(res, key=lambda x: -x[1])[:3]
        mins = len(audio) * STEP / 60
        print(
            f"{login}: {mins:.1f} min, chat {int(chat.sum())} msgs, best chat lag {lag:.1f}s "
            f"(r={r:.2f}); top {[(x, round(y, 2)) for x, y in top3]}; "
            f"delay_s used: {config.streamer(login).get('delay_s', 8)}"
        )


if __name__ == "__main__":
    main()
