"""Encoder profiles (D4). Availability is probed once; unknown/unavailable profiles fall back."""

from __future__ import annotations

import subprocess
from functools import lru_cache

from clipwatcher.config import ffmpeg_bin

_X264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "high"]

PROFILES: dict[str, list[str]] = {
    "cpu_batch": [*_X264, "-preset", "slow", "-crf", "18", "-maxrate", "16M", "-bufsize", "32M"],
    "cpu_live": [*_X264, "-preset", "veryfast", "-crf", "20", "-maxrate", "16M", "-bufsize", "32M"],
    "nvenc": [
        *["-c:v", "h264_nvenc", "-pix_fmt", "yuv420p", "-profile:v", "high"],
        *["-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0", "-maxrate", "16M", "-bufsize", "32M"],
    ],
    "mac_preview": ["-c:v", "h264_videotoolbox", "-pix_fmt", "yuv420p", "-b:v", "12M"],
}

_REQUIRES = {"nvenc": "h264_nvenc", "mac_preview": "h264_videotoolbox"}


@lru_cache
def _encoder_list() -> str:
    try:
        r = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30
        )
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def available(profile: str) -> bool:
    if profile not in PROFILES:
        return False
    need = _REQUIRES.get(profile)
    return need is None or need in _encoder_list()


def resolve(profile: str) -> str:
    return profile if available(profile) else "cpu_batch"


def video_args(profile: str) -> list[str]:
    return list(PROFILES[resolve(profile)])
