"""Standardised, token-lean judge input and output.

Input: a static, cached system prompt (prompts/judge_v2.md) + labelled low-res frames + one compact JSON
object (meta, transcript phrases, chat summary). Output: a short-key JSON object (OUTPUT_SCHEMA) that is
clamped and normalised in code, because structured outputs cannot enforce string lengths or number ranges.
"""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .. import config
from ..models import Word

SYSTEM_PROMPT_FILE = config.ROOT / "prompts" / "judge_v2.md"
PHRASE_GAP_S = 0.6
PHRASE_MAX_WORDS = 10
CAPS = {"hk": 35, "tt": 60, "ds": 220, "why": 80}
_SENT_END = re.compile(r"[.!?…]$")

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "sc": {"type": "integer", "enum": list(range(1, 11))},
        "t0": {"type": "number"},
        "t1": {"type": "number"},
        "hk": {"type": "string"},
        "tt": {"type": "string"},
        "ds": {"type": "string"},
        "fl": {
            "type": "object",
            "properties": {"sp": {"type": "boolean"}, "mu": {"type": "boolean"}, "gb": {"type": "boolean"}},
            "required": ["sp", "mu", "gb"],
            "additionalProperties": False,
        },
        "why": {"type": "string"},
    },
    "required": ["ok", "sc", "t0", "t1", "hk", "tt", "ds", "fl", "why"],
    "additionalProperties": False,
}


def system_prompt() -> str:
    """The static system prompt. Never formatted with per-clip data, so the cached prefix never changes."""
    return SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")


# -- transcript --------------------------------------------------------------------------------------
def _words_from(transcript: Any) -> list[tuple[float, float, str]]:
    """Accept list[Word], a faster-whisper/WhisperX JSON dict or a list of segment/word dicts."""
    if isinstance(transcript, dict):
        transcript = transcript.get("segments") or transcript.get("words") or []
    out: list[tuple[float, float, str]] = []
    for item in transcript or []:
        if isinstance(item, Word):
            out.append((item.start, item.end, item.text))
        elif isinstance(item, dict) and item.get("words"):
            out.extend(_words_from(item["words"]))
        elif isinstance(item, dict):
            out.append(
                (
                    float(item.get("start", 0)),
                    float(item.get("end", 0)),
                    str(item.get("text") or item.get("word") or ""),
                )
            )
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            out.append((float(item[0]), float(item[1]), str(item[2])))
    return [(s, e, t.strip()) for s, e, t in out if t and t.strip()]


def serialize_transcript_compact(transcript: Any) -> list[list[Any]]:
    """[[start, end, "phrase"], ...] with 1-decimal times; drops confidences, probabilities and per-word keys.

    Words are joined into phrases at pauses > 0.6 s, sentence ends, or every 10 words.
    """
    phrases: list[list[Any]] = []
    cur: list[tuple[float, float, str]] = []

    def flush() -> None:
        if cur:
            phrases.append([round(cur[0][0], 1), round(cur[-1][1], 1), " ".join(t for _, _, t in cur)])
            cur.clear()

    for s, e, t in _words_from(transcript):
        if cur and (s - cur[-1][1] > PHRASE_GAP_S or len(cur) >= PHRASE_MAX_WORDS):
            flush()
        cur.append((s, e, t))
        if _SENT_END.search(t):
            flush()
    flush()
    return phrases


# -- chat --------------------------------------------------------------------------------------------
def _norm_msg(text: str, max_len: int) -> str:
    t = " ".join(text.split())
    return t if len(t) <= max_len else t[: max_len - 1] + "…"


def compact_chat(chat: Iterable[Any], top: int = 12, max_len: int = 40) -> dict[str, Any]:
    """{"n": messages, "u": unique chatters, "top": [[message, count], ...]}; no usernames, no repeats.

    Accepts "user: text" lines (what the lanes build) or ChatMessage objects.
    """
    users: set[str] = set()
    counts: Counter[str] = Counter()
    first: dict[str, str] = {}
    n = 0
    for m in chat:
        if hasattr(m, "text"):
            user, text = getattr(m, "user", ""), m.text
        else:
            user, _, text = str(m).partition(": ") if ": " in str(m) else ("", "", str(m))
        text = text.strip()
        if not text:
            continue
        n += 1
        users.add(user)
        key = " ".join(text.casefold().split())
        counts[key] += 1
        first.setdefault(key, text)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return {"n": n, "u": len(users), "top": [[_norm_msg(first[k], max_len), c] for k, c in ranked]}


# -- payload -----------------------------------------------------------------------------------------
def copy_language(spoken: str | None) -> str:
    jcfg = config.global_config().get("judge", {})
    forced = str(jcfg.get("copy_language", "auto"))
    if forced in ("it", "en"):
        return forced
    lang = (spoken or "").lower()[:2]
    return lang if lang in ("it", "en") else str(jcfg.get("default_copy_language", "it"))


def build_payload(
    *,
    streamer: str,
    platform: str,
    game: str,
    language: str | None,
    dur: float,
    peak_s: float | None,
    transcript: Any,
    chat: Iterable[Any],
    task: str = "judge",
) -> dict[str, Any]:
    meta = {
        "s": streamer.lower(),
        "p": platform,
        "g": game,
        "l": (language or "?")[:2],
        "cl": copy_language(language),
        "dur": round(float(dur), 1),
    }
    if peak_s is not None:
        meta["pk"] = round(float(peak_s), 1)
    return {
        "meta": meta,
        "tr": serialize_transcript_compact(transcript),
        "ch": compact_chat(chat),
        "task": task,
    }


def payload_json(payload: dict[str, Any]) -> str:
    """Minified, deterministic JSON (no spaces, UTF-8 emoji kept as-is)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def frame_blocks(frames: list[tuple[float, Path]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for t, p in frames:
        blocks.append({"type": "text", "text": f"[t={t:.1f}s]"})
        data = base64.standard_b64encode(Path(p).read_bytes()).decode()
        blocks.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}
        )
    return blocks


def thinking_for(model: str) -> dict[str, Any] | None:
    """Thinking config per model from judge.thinking (thinking bills as output; this task needs none)."""
    t = (config.global_config().get("judge", {}).get("thinking") or {}).get(model)
    return {"type": t} if t else None


def build_request(
    model: str, frames: list[tuple[float, Path]], payload: dict[str, Any], max_tokens: int | None = None
) -> dict[str, Any]:
    """kwargs for client.messages.create: cached static system, frames, then the compact JSON last."""
    jcfg = config.global_config().get("judge", {})
    req: dict[str, Any] = {
        "model": model,
        "max_tokens": int(max_tokens or jcfg.get("max_tokens", 1024)),
        "system": [{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {
                "role": "user",
                "content": [*frame_blocks(frames), {"type": "text", "text": payload_json(payload)}],
            }
        ],
        "output_config": {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    }
    if jcfg.get("effort"):
        req["output_config"]["effort"] = jcfg["effort"]
    th = thinking_for(model)
    if th:
        req["thinking"] = th
    return req


# -- output ------------------------------------------------------------------------------------------
def _cap(s: Any, n: int) -> str:
    s = " ".join(str(s).split()) if n != CAPS["ds"] else str(s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def credit(login: str, platform: str) -> str:
    return f"{'kick.com' if platform == 'kick' else 'twitch.tv'}/{login.lower()}"


def normalize_output(data: dict[str, Any], *, login: str, platform: str, dur: float) -> dict[str, Any]:
    """Clamp scores/times, enforce character caps and the streamer credit, recompute ok from flags.

    Raises ValueError when a required key is missing or the hook/title is empty (caller falls back).
    """
    missing = [k for k in OUTPUT_SCHEMA["required"] if k not in data]
    if missing:
        raise ValueError(f"missing keys {missing}")
    if not str(data["hk"]).strip() or not str(data["tt"]).strip():
        raise ValueError("empty hook or title")
    fl = {k: bool((data.get("fl") or {}).get(k, False)) for k in ("sp", "mu", "gb")}
    sc = max(1, min(10, int(round(float(data.get("sc", 1))))))
    t0 = max(0.0, min(float(dur), float(data.get("t0", 0.0))))
    t1 = max(0.0, min(float(dur), float(data.get("t1", dur))))
    if t1 <= t0:
        t0, t1 = 0.0, float(dur)
    want = credit(login, platform)
    lines = [ln.strip() for ln in str(data.get("ds", "")).split("\n") if ln.strip()]
    if not any(want in ln for ln in lines):
        lines = [(lines[0] + " " if lines else "") + f"🎥 Live: {want}", *lines[1:]]
    ds = "\n".join(lines)
    if len(ds) > CAPS["ds"]:  # keep the credit line and hashtags; shorten the teaser
        head, rest = lines[0], "\n".join(lines[1:])
        room = CAPS["ds"] - len(rest) - 1
        if room > len(want) + 10:
            teaser = head.replace(f"🎥 Live: {want}", "").strip()
            keep = room - len(f" 🎥 Live: {want}")
            head = (
                teaser[: max(0, keep - 1)].rstrip() + "…" if len(teaser) > keep else teaser
            ) + f" 🎥 Live: {want}"
            ds = head + "\n" + rest
        else:
            ds = ds[: CAPS["ds"]]
    reject = config.global_config().get("judge", {}).get("reject_flags", ["sp", "mu", "gb"])
    ok = bool(data.get("ok", False)) and not any(fl[k] for k in reject if k in fl)
    return {
        "ok": ok,
        "sc": sc,
        "t0": round(t0, 1),
        "t1": round(t1, 1),
        "hk": _cap(data.get("hk", ""), CAPS["hk"]),
        "tt": _cap(data.get("tt", ""), CAPS["tt"]),
        "ds": ds,
        "fl": fl,
        "why": _cap(data.get("why", ""), CAPS["why"]),
    }
