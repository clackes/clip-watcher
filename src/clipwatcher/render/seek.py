"""D9 seek for ffmpeg commands that decode only the picture or only the sound of a file.

A render decodes both streams, and ffmpeg then counts time from the start of the file: second 0 is the
first sample of whichever stream starts first. With only one stream decoded ffmpeg counts differently as
soon as the other, unused stream is the one that starts first (MPEG-TS, measured on ffmpeg 7.1): from the
decoded stream's own start when the input seek is 0, and from the raw timestamps minus the seek when it is
larger, i.e. off by the file's start time. That is 1.4 s on a ring-buffer cut: judge frames showed the
picture 1.4 s before their label, words and loudness landed 1.4 s late.

So single-stream reads never rely on ffmpeg's zero: they keep the file's own timestamps (-copyts) and trim
on those, file start + position. Coarse input seek, exact trim in the graph, then setpts=PTS-STARTPTS.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from clipwatcher import config

PAD_S = 6.0  # the coarse seek lands this far before the position; the graph trims the rest


@lru_cache(maxsize=64)
def _start(path: str, mtime_ns: int, size: int) -> float | None:
    try:
        r = subprocess.run(
            [config.ffprobe_bin(), "-v", "error", "-show_entries", "format=start_time", "-of",
             "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        return float(r.stdout.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def file_start(media: str | Path) -> float | None:
    """Timestamp of the first sample of the file (its earliest stream), None when ffprobe cannot tell."""
    try:
        st = Path(media).stat()
    except OSError:
        return None
    return _start(str(media), st.st_mtime_ns, st.st_size)


def one_stream(media: str | Path, pos_s: float, pad_s: float = PAD_S) -> tuple[list[str], float]:
    """(ffmpeg input arguments, trim start) to read one stream of media from pos_s seconds into the file,
    on the same clock a render uses. Use as [ffmpeg, ..., *args, "-vf", f"trim=start={t},setpts=..."].

    The input seek goes pad_s before the position, so a single frame costs a few seconds of decoding even
    far into a long file."""
    coarse = max(0.0, pos_s - pad_s)
    t0 = file_start(media)
    if t0 is None:  # unknown start: ffmpeg's own zero (right whenever the decoded stream starts first)
        return ["-ss", f"{coarse:.3f}", "-i", str(media)], pos_s - coarse
    return ["-copyts", "-ss", f"{coarse:.3f}", "-i", str(media)], t0 + pos_s
