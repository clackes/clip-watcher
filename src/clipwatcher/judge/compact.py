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
from .. import sfx as sfx_catalog
from ..models import Word

SYSTEM_PROMPT_FILE = config.ROOT / "prompts" / "judge_v2.md"
PHRASE_GAP_S = 0.6
PHRASE_MAX_WORDS = 10
CAPS = {"hk": 70, "tt": 60, "ds": 300, "why": 80, "lb": 16}
FX_KINDS = ("hype", "fail", "shock", "funny")  # beat kinds -> label colour + screen effect
FX_MAX = 3
SFX_NAMES = list(sfx_catalog.CATALOG)
_SENT_END = re.compile(r"[.!?…]$")

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "sc": {"type": "integer", "enum": list(range(1, 11))},
        "t0": {"type": "number"},
        "t1": {"type": "number"},
        "pk": {"type": "number"},
        "hk": {"type": "string"},
        "tt": {"type": "string"},
        "ds": {"type": "string"},
        "fl": {
            "type": "object",
            "properties": {"sp": {"type": "boolean"}, "mu": {"type": "boolean"}, "gb": {"type": "boolean"}},
            "required": ["sp", "mu", "gb"],
            "additionalProperties": False,
        },
        "cm": {"type": "array", "items": {"type": "number"}},
        "fx": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "t": {"type": "number"},
                    "k": {"type": "string", "enum": list(FX_KINDS)},
                    "lb": {"type": "string"},
                    "sx": {"type": "string", "enum": ["", *SFX_NAMES]},
                },
                "required": ["t", "k", "lb", "sx"],
                "additionalProperties": False,
            },
        },
        "ac": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t0": {"type": "number"}, "t1": {"type": "number"}, "tx": {"type": "string"}},
                "required": ["t0", "t1", "tx"],
                "additionalProperties": False,
            },
        },
        "why": {"type": "string"},
    },
    "required": ["ok", "sc", "t0", "t1", "pk", "hk", "tt", "ds", "fl", "cm", "fx", "ac", "why"],
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
        if " " in t:  # already a phrase (segment-level input): keep it as its own entry
            flush()
            cur.append((s, e, t))
            flush()
            continue
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


_MENTION = re.compile(r"^(@\S+[\s,:]*)+")


def compact_chat(chat: Iterable[Any], top: int = 12, max_len: int = 32, singles: int = 4) -> dict[str, Any]:
    """{"n": messages, "u": unique chatters, "top": [[message, count], ...]}; no usernames, no repeats.

    Repeated messages (the reaction signal) come first; at most `singles` one-off messages fill in.
    Commands (!time) are dropped and leading @mentions stripped. Accepts "user: text" lines or ChatMessages.
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
        text = _MENTION.sub("", text.strip())
        if not text or text.startswith("!"):
            continue
        n += 1
        users.add(user)
        key = " ".join(text.casefold().split())
        counts[key] += 1
        first.setdefault(key, text)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    rep = [kv for kv in ranked if kv[1] > 1]
    ranked = (rep + [kv for kv in ranked if kv[1] == 1][:singles])[:top]
    return {"n": n, "u": len(users), "top": [[_norm_msg(first[k], max_len), c] for k, c in ranked]}


# -- payload -----------------------------------------------------------------------------------------
def copy_language(spoken: str | None = None) -> str:
    """Copy is always English: ClipVault is an English-only channel."""
    return "en"


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
    loud: list[tuple[float, float]] | None = None,
    note: str = "",
) -> dict[str, Any]:
    meta = {
        "s": streamer.lower(),
        "p": platform,
        "g": game,
        "l": (language or "?")[:2],
        "dur": round(float(dur), 1),
    }
    if peak_s is not None:
        meta["pk"] = round(float(peak_s), 1)
    if note:  # operator's note on a redo ("start earlier", "cut the ending")
        meta["note"] = note[:300]
    out = {
        "meta": meta,
        "tr": serialize_transcript_compact(transcript),
        "ch": compact_chat(chat),
        "task": task,
    }
    if loud is not None:  # sudden loudness jumps: screams, crashes, laughs, even when nothing is transcribed
        out["au"] = [[round(t, 1), round(db)] for t, db in loud]
    return out


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
    """Thinking config per model from judge.thinking. Thinking bills as output, but without it the judge
    scored the same clip anywhere from 5 to 8 (temperature is not settable on these models)."""
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


# Generic engagement begging ("like, comment and subscribe", "follow for more", "Subscribe!"): viewers skip
# it and platforms down-rank it, so such lines never reach a description (D10). Bait is an order to the
# viewer. A line that tells the clip and happens to use the words ("he looked like he'd follow chat
# anywhere") is not: counting like/comment/follow words dropped those and left three lines of four.
_ASK = r"(?:like|comment|subscribe|sub|follow|share)"
_CLAUSE = r"(?:^|[.!?…:;]\s*|[\u2600-\u27bf\U0001f300-\U0001faff]\ufe0f?\s*)"  # start of a line or clause
_BAIT = re.compile(
    "|".join(
        (
            rf"\b{_ASK}s?\s*(?:,|&|\+|/|\band\b)\s*(?:and\s+)?{_ASK}\b",  # like and subscribe; comment, like
            r"\b(?:smash|hit|tap|press|mash|spam|click)\s+(?:that\s+|the\s+|a\s+)?"
            r"(?:like|sub|subscribe|follow|bell|notif)",
            r"\b(?:drop|leave|give)\s+(?:us\s+|me\s+|it\s+|this\s+)?(?:a|your)\s+(?:like|follow|sub|comment)s?\b",
            rf"\b(?:don'?t|do\s+not)\s+forget\s+to\s+{_ASK}\b",
            rf"\b(?:make\s+sure|be\s+sure|remember)\s+(?:to|you)\s+{_ASK}\b",
            rf"\b(?:please|pls|plz)\s+{_ASK}\b",
            r"\b(?:follow|subscribe|sub)\b[^.!?…]{0,30}?\bfor\s+(?:more|daily|part)\b(?!\s+than\b)",
            r"\bcomment\s+(?:down\s+)?below\b",
            # an order opening the line or a clause of it
            rf"{_CLAUSE}(?:subscribe|sub\s+to)\b",
            rf"{_CLAUSE}follow(?:\s+(?:so|to|if|us|me|now|today|and|along|here|(?:the|this|my|our)\s+channel)\b|\W*$)",
            rf"{_CLAUSE}comment\b(?!\s+(?:sections?|of|on)\b)",
            rf"{_CLAUSE}share(?:\s+(?:this|it|with|to|if|now)\b|\W*$)",
            rf"{_CLAUSE}like(?:\s+(?:if|for\s+(?:more|part)|(?:this|the)\s+(?:video|clip|short|reel|post))\b|\W*$)",
        )
    ),
    re.I,
)


def is_bait(line: str) -> bool:
    """True when the line asks the viewer to like, comment, follow, subscribe or share: an order at the
    start of the line or of a clause ("Subscribe!", "Like if you laughed"), two asks in a row ("like and
    subscribe") or a stock phrase ("smash that like", "follow for more", "comment below"). Hashtag lines
    are never bait."""
    if line.lstrip().startswith("#"):
        return False
    return bool(_BAIT.search(line.strip()))


def _fit_description(lines: list[str], want: str) -> str:
    """Join lines within CAPS["ds"]: shorten the line-1 teaser first, keep credit, question and hashtags."""
    ds = "\n".join(lines)
    if len(ds) <= CAPS["ds"]:
        return ds
    tag = f" 🎥 {want}"
    head = lines[0]
    teaser = head[: head.index(want)].replace("🎥", "").replace("Live:", "").strip() if want in head else head
    rest = "\n".join(lines[1:])
    keep = CAPS["ds"] - len(rest) - 1 - len(tag)
    if keep >= 10:
        teaser = teaser if len(teaser) <= keep else teaser[: keep - 1].rstrip() + "…"
        return teaser + tag + ("\n" + rest if rest else "")
    # Still too long: drop the promise line (line 3 of 4), then hard-cut as a last resort.
    if len(lines) >= 4:
        return _fit_description([lines[0], lines[1], lines[-1]], want)
    return ds[: CAPS["ds"]]


def _stock_line(place: int, login: str, seed: int) -> str:
    """The fallback copy's line for one place of the 4-line description (teaser, question, promise, tags)."""
    n = login[:1].upper() + login[1:]
    opts = (_FB_TEASERS, _FB_QUESTIONS, _FB_PROMISES, (f"#{n} #Clip #Shorts",))[place]
    return opts[seed % len(opts)].format(n=n)


def build_description(lines: list[str], login: str, platform: str) -> str:
    """Drop engagement bait, make sure line 1 carries the streamer credit, fit the length cap.

    A description has four lines (D10). When the judge wrote all four and one of them begs, that line is
    replaced by the stock line for its place, so the description keeps its shape; in any other shape bait
    is only removed."""
    want = credit(login, platform)
    lines = [ln.strip() for ln in lines if ln.strip()]
    if len(lines) == 4:
        lines = [
            _stock_line(i, login, sum(map(ord, login + ln))) if is_bait(ln) else ln
            for i, ln in enumerate(lines)
        ]
    else:
        lines = [ln for ln in lines if not is_bait(ln)]
    if not any(want in ln for ln in lines):
        lines = [(lines[0] + " " if lines else "") + f"🎥 {want}", *lines[1:]]
    return _fit_description(lines, want)


_FB_HOOKS = (
    "Chat was NOT ready for this 💀",
    "Bro did not see that coming 😭",
    "Watch his face at the end 💀",
    "This went wrong so fast 😭🙏",
)
_FB_TITLES = (
    "{n} did NOT expect this... 💀",
    "Chat saw it before {n} did 😭",
    "{n} wasn't ready for this...",
)
_FB_TEASERS = ("Chat went off for a reason", "Nobody saw the ending coming", "Watch it to the last second")
_FB_QUESTIONS = ("Rate that reaction 1-10 👇", "Skill or pure luck? 👇", "Would you have seen it coming? 👇")
_FB_PROMISES = ("Every wild {n} moment lands here first 🔔", "The best {n} moments, every single day 🔔")


def hashtag(text: str) -> str:
    """'GTA VI' -> 'GTA6' style tag body: alphanumerics only, roman numeral suffixes as digits."""
    roman = {"II": "2", "III": "3", "IV": "4", "V": "5", "VI": "6", "VII": "7"}
    words = str(text).split()
    if len(words) > 1 and words[-1].upper() in roman:
        words[-1] = roman[words[-1].upper()]
    return "".join(ch for ch in "".join(words) if ch.isalnum())


def normalize_fx(fx: Any, t0: float, t1: float) -> list[tuple[float, str, str, str]]:
    """Beats (t, kind, label, sound) inside [t0, t1], known kinds only, at most FX_MAX, >= 1.5 s apart,
    labels capped, unknown sounds dropped to "" (no sound)."""
    out: list[tuple[float, str, str, str]] = []
    for b in fx if isinstance(fx, list) else []:
        try:
            t, k, lb = float(b["t"]), str(b["k"]), str(b.get("lb", ""))
        except (KeyError, TypeError, ValueError):
            continue
        if k not in FX_KINDS or not t0 <= t <= t1 or any(abs(t - o[0]) < 1.5 for o in out):
            continue
        lb = _cap(lb.strip().upper(), CAPS["lb"])
        if lb and not lb.startswith("*"):
            lb = f"*{lb.strip('*')}*"
        sx = str(b.get("sx", "") or "")
        out.append((round(t, 2), k, lb, sx if sx in sfx_catalog.CATALOG else ""))
        if len(out) >= FX_MAX:
            break
    return sorted(out)


def normalize_ac(ac: Any, t0: float, t1: float) -> list[tuple[float, float, str]]:
    """Action labels (start, end, *TEXT*) clamped to [t0, t1], >= 0.8 s long, no overlaps, at most 3."""
    out: list[tuple[float, float, str]] = []
    for a in ac if isinstance(ac, list) else []:
        try:
            s, e, tx = max(t0, float(a["t0"])), min(t1, float(a["t1"])), str(a["tx"]).strip().upper()
        except (KeyError, TypeError, ValueError):
            continue
        tx = _cap(tx.strip("*"), CAPS["lb"] + 6)
        if not tx or e - s < 0.8 or any(s < oe and os < e for os, oe, _ in out):
            continue
        out.append((round(s, 2), round(e, 2), f"*{tx}*"))
        if len(out) >= 3:
            break
    return sorted(out)


def fallback_copy(login: str, platform: str, game: str | None, seed: int) -> dict[str, str]:
    """English hook/title/description for clips the judge could not write (no key, API error).

    Same 4-line description shape as the judge: teaser + credit, a question, a reason to follow, hashtags.
    """
    n = login[:1].upper() + login[1:]
    pick = lambda opts: opts[seed % len(opts)]  # noqa: E731
    tags = [f"#{n}"] + ([f"#{hashtag(game)}"] if game and hashtag(game) else []) + ["#Clip", "#Shorts"]
    lines = [pick(_FB_TEASERS), pick(_FB_QUESTIONS), pick(_FB_PROMISES).format(n=n), " ".join(tags)]
    return {
        "hk": pick(_FB_HOOKS),
        "tt": _cap(pick(_FB_TITLES).format(n=n), CAPS["tt"]),
        "ds": build_description(lines, login, platform),
    }


def normalize_output(data: dict[str, Any], *, login: str, platform: str, dur: float) -> dict[str, Any]:
    """Clamp scores/times, enforce character caps and the streamer credit, recompute ok from flags.

    Raises ValueError when a required key is missing or the hook/title is empty (caller falls back).
    """
    missing = [k for k in OUTPUT_SCHEMA["required"] if k not in data and k not in ("pk", "cm", "fx", "ac")]
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
    pk = data.get("pk")
    pk = round(min(max(float(pk), t0), t1), 1) if isinstance(pk, (int, float)) else None
    ds = build_description(str(data.get("ds", "")).split("\n"), login, platform)
    cm = data.get("cm")
    cm = (
        (round(float(cm[0]), 3), round(float(cm[1]), 3))
        if isinstance(cm, list)
        and len(cm) == 2
        and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in cm)
        else None
    )
    fx = normalize_fx(data.get("fx"), t0, t1)
    ac = normalize_ac(data.get("ac"), t0, t1)
    reject = config.global_config().get("judge", {}).get("reject_flags", ["sp"])
    ok = bool(data.get("ok", False)) and not any(fl[k] for k in reject if k in fl)
    return {
        "ok": ok,
        "sc": sc,
        "t0": round(t0, 1),
        "t1": round(t1, 1),
        "pk": pk,
        "hk": _cap(data.get("hk", ""), CAPS["hk"]),
        "tt": _cap(data.get("tt", ""), CAPS["tt"]),
        "ds": ds,
        "fl": fl,
        "cm": cm,
        "fx": fx,
        "ac": ac,
        "why": _cap(data.get("why", ""), CAPS["why"]),
    }
