"""Config loading and tool paths."""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("CLIPWATCHER_CONFIG", ROOT / "config"))


def dry_run() -> bool:
    """DRY_RUN defaults to ON. Only DRY_RUN=0 turns it off."""
    return os.environ.get("DRY_RUN", "1") != "0"


def _load(name: str, example: str | None = None) -> dict[str, Any]:
    p = CONFIG_DIR / name
    if not p.exists() and example:
        p = CONFIG_DIR / example
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


@lru_cache
def global_config() -> dict[str, Any]:
    return _load("global.yaml")


def streamers() -> dict[str, Any]:
    return _load("streamers.yaml", "streamers.example.yaml").get("streamers", {})


def operators() -> dict[str, Any]:
    return _load("operators.yaml", "operators.example.yaml").get("operators", {})


def streamer(name: str) -> dict[str, Any]:
    return streamers().get(name.lower(), {})


def channel_language() -> str:
    """The one language the channel publishes in (channel.language, default en)."""
    return str(global_config().get("channel", {}).get("language", "en")).lower()


def stream_language(name: str) -> str:
    """Spoken language of a streamer (streamers.yaml language, default: the channel language)."""
    return str(streamer(name).get("language") or channel_language()).lower()


def allowed_streamer(name: str) -> bool:
    """Only streamers who speak the channel language are watched and clipped."""
    return stream_language(name) == channel_language()


def keywords_for(name: str | None) -> dict[str, float]:
    """D3: global reaction keyword weights merged with per-streamer extras."""
    kw = dict(global_config().get("detector", {}).get("keywords", {}))
    if name:
        kw.update(streamer(name).get("keywords", {}))
    return kw


def _first_existing(*cands: str | None) -> str | None:
    for c in cands:
        if c and (Path(c).exists() or shutil.which(c)):
            return shutil.which(c) or c
    return None


@lru_cache
def streamlink_bin() -> str:
    """streamlink from env/config, next to this Python (the venv), then PATH.

    The venv's bin/ is not on PATH unless the venv is activated, and a bare "streamlink" then made every
    recorder exit at once.
    """
    import sys

    found = _first_existing(
        os.environ.get("CLIPWATCHER_STREAMLINK"),
        global_config().get("tools", {}).get("streamlink"),
        str(Path(sys.executable).parent / "streamlink"),
        "streamlink",
    )
    if not found:
        raise RuntimeError("streamlink not found")
    return found


def ffmpeg_bin() -> str:
    """Prefer ffmpeg-full (has libass), then config/env, then PATH."""
    found = _first_existing(
        os.environ.get("CLIPWATCHER_FFMPEG"),
        global_config().get("tools", {}).get("ffmpeg"),
        "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
        "ffmpeg",
    )
    if not found:
        raise RuntimeError("ffmpeg not found")
    return found


@lru_cache
def ffprobe_bin() -> str:
    ff = Path(ffmpeg_bin())
    sib = ff.with_name("ffprobe" + ff.suffix)  # ffprobe.exe next to ffmpeg.exe on Windows
    return str(sib) if sib.exists() else (shutil.which("ffprobe") or "ffprobe")


def twitch_downloader_bin() -> str:
    return (
        os.environ.get("CLIPWATCHER_TDL")
        or global_config().get("tools", {}).get("twitch_downloader")
        or str(ROOT / "bin" / "TwitchDownloaderCLI")
    )


def credit(name: str) -> str:
    """On-screen / metadata credit for a streamer: twitch.tv/<name> or kick.com/<name>."""
    plat = streamer(name).get("platform", "twitch")
    return f"{'kick.com' if plat == 'kick' else 'twitch.tv'}/{name.lower()}"
