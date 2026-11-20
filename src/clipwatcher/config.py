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
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _merged(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """``base`` with ``over`` laid on top; a mapping inside a mapping is merged, anything else replaced."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def local_overrides() -> dict[str, Any]:
    """config/local.yaml: this machine's own settings, laid over global.yaml (the companion app writes it;
    it is not committed). CLIPWATCHER_LOCAL=0 ignores it, which is what the tests do."""
    return {} if os.environ.get("CLIPWATCHER_LOCAL", "1") == "0" else _load("local.yaml")


@lru_cache
def global_config() -> dict[str, Any]:
    return _merged(_load("global.yaml"), local_overrides())


def save_local(overrides: dict[str, Any]) -> Path:
    """Replace config/local.yaml with ``overrides`` (an empty mapping removes the file). Takes effect for
    this process at once and for a running bot at its next start."""
    p = CONFIG_DIR / "local.yaml"
    if overrides:
        head = (
            "# Settings of this machine, laid over global.yaml. Written by the companion app\n"
            "# (python -m clipwatcher.ui); delete a line, or the file, to go back to global.yaml.\n"
        )
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(head + yaml.safe_dump(overrides, sort_keys=True, allow_unicode=True), encoding="utf-8")
        os.replace(tmp, p)
    elif p.exists():
        p.unlink()
    global_config.cache_clear()
    return p


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


_LEARNED: tuple[float, dict[str, Any]] = (0.0, {})


def learned() -> dict[str, Any]:
    """config/keywords.yaml: what scripts/learn_keywords.py found in real chat (D16). Re-read when the
    file changes: a running bot uses a new learn run from the next detector it makes (a new broadcast, a
    change of game, a restart)."""
    global _LEARNED
    p = CONFIG_DIR / "keywords.yaml"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if _LEARNED[0] != mtime:
        _LEARNED = (mtime, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    return _LEARNED[1]


def category_profile(category: str | None) -> tuple[str | None, dict[str, Any]]:
    """(name, settings) of the kind of stream a platform's category belongs to, or (None, {}).

    Kinds are listed under ``categories:`` in global.yaml (hand-set: what to match, detector overrides,
    extra keywords) and in keywords.yaml (learned keywords). A stream belongs to the first kind one of whose
    ``match`` texts is in the category name: "grand theft auto vi" is in Twitch's "Grand Theft Auto VI" and
    in Kick's "Grand Theft Auto VI (GTA)". In the settings ``keywords`` are the hand-set ones and
    ``learned_keywords`` the learned.
    """
    if not category:
        return None, {}
    hand, auto = global_config().get("categories") or {}, learned().get("categories") or {}
    text = category.lower()
    for name in [*hand, *(k for k in auto if k not in hand)]:
        prof_h, prof_a = hand.get(name) or {}, auto.get(name) or {}
        needles = [*(prof_h.get("match") or []), *(prof_a.get("match") or [])] or [name.replace("-", " ")]
        if any(str(n).lower() in text for n in needles):
            return name, {
                **prof_a, **prof_h, "keywords": prof_h.get("keywords") or {},
                "learned_keywords": prof_a.get("keywords") or {},
            }  # fmt: skip
    return None, {}


def _add_keywords(kw: dict[str, float], extra: dict[str, Any] | None, *, raise_only: bool = False) -> None:
    """Put ``extra`` into ``kw``, a word being the same word in any case (KEKW, kekw). ``raise_only``: a
    weight found by the learner never lowers one a person set."""
    spelt = {k.lower(): k for k in kw}
    for word, w in (extra or {}).items():
        have = spelt.get(str(word).lower())
        if have is None:
            kw[str(word)] = float(w)
            spelt[str(word).lower()] = str(word)
        elif not raise_only or float(w) > kw[have]:
            kw[have] = float(w)


def keywords_for(name: str | None, category: str | None = None) -> dict[str, float]:
    """Reaction keyword weights for one stream (D3, D16): the global list; the words learned for the kind of
    stream it is (``category``, as the platform names it; the streamer's ``game`` in streamers.yaml when not
    given) and that kind's hand-set ones; the words learned for this streamer's chat and the streamer's
    hand-set extras. Hand-set weights win; a learned weight only adds a word or raises one."""
    kw: dict[str, float] = {}
    _add_keywords(kw, global_config().get("detector", {}).get("keywords"))
    scfg = streamer(name) if name else {}
    kind = category_profile(category or scfg.get("game"))[1]
    _add_keywords(kw, kind.get("learned_keywords"), raise_only=True)
    _add_keywords(kw, kind.get("keywords"))
    if name:
        own = ((learned().get("streamers") or {}).get(name.lower()) or {}).get("keywords")
        _add_keywords(kw, own, raise_only=True)
        _add_keywords(kw, scfg.get("keywords"))
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
