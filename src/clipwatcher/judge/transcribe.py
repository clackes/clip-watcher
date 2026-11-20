"""Word-level transcription of a candidate window (faster-whisper, optional)."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import config
from ..models import Word
from ..render import seek

log = logging.getLogger(__name__)


def extract_wav(media: str | Path, start_s: float, dur_s: float, out: Path) -> Path:
    """16 kHz mono wav of the window. Coarse input seek, then atrim in the graph (D9, render/seek.py)."""
    inp, trim = seek.one_stream(media, start_s)
    af = f"atrim=start={trim:.3f}:duration={dur_s:.3f},asetpts=PTS-STARTPTS"
    cmd = [
        config.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        *inp, "-vn", "-af", af, "-ac", "1", "-ar", "16000", str(out),
    ]  # fmt: skip
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def load_wav(path: Path) -> Any:
    """16 kHz mono s16 WAV -> float32 array. Passing an array keeps faster-whisper away from PyAV, whose
    newer releases dropped an argument faster-whisper 1.2.1 still passes."""
    import wave

    import numpy as np

    with wave.open(str(path)) as w:
        pcm = w.readframes(w.getnframes())
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _model() -> Any:
    return _load_model(config.global_config().get("judge", {}).get("whisper_model", "large-v3-turbo"))


@lru_cache(maxsize=2)
def _load_model(name: str) -> Any:
    """Load each Whisper model once per process (loading large-v3-turbo takes seconds and ~1.5 GB)."""
    from faster_whisper import WhisperModel

    return WhisperModel(name, compute_type="int8")


def transcribe(
    media: str | Path,
    start_s: float,
    dur_s: float,
    language: str | None,
    initial_prompt: str | None = None,
) -> list[Word]:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        log.warning("faster-whisper not installed; skipping transcription")
        return []
    with tempfile.TemporaryDirectory() as td:
        wav = extract_wav(media, start_s, dur_s, Path(td) / "w.wav")
        segments, _info = _model().transcribe(
            load_wav(wav),
            word_timestamps=True,
            vad_filter=True,
            language=language,
            initial_prompt=initial_prompt,
        )
        words: list[Word] = []
        for seg in segments:
            for w in seg.words or []:
                t = w.word.strip()
                if t:
                    words.append(Word(start=float(w.start), end=float(w.end), text=t))
    return words
