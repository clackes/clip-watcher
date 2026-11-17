"""Screening and judging of candidates with structured outputs and a heuristic fallback."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from .. import config
from ..models import Candidate, JudgeResult, Word
from .snap import heuristic_trim, snap_trim

log = logging.getLogger(__name__)
PROMPTS = config.ROOT / "prompts"

_NUM = {"type": "number"}
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "funny_score": _NUM,
        "hype_score": _NUM,
        "visual_context_summary": {"type": "string"},
        "trim_start_s": _NUM,
        "trim_end_s": _NUM,
        "hook_text": {"type": "string"},
        "title_options": {"type": "array", "items": {"type": "string"}},
        "setup_line": {"type": "string"},
        "reject_reason": {"type": ["string", "null"]},
    },
    "required": [
        "funny_score",
        "hype_score",
        "visual_context_summary",
        "trim_start_s",
        "trim_end_s",
        "hook_text",
        "title_options",
        "setup_line",
        "reject_reason",
    ],  # fmt: skip
    "additionalProperties": False,
}
SCREEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "funny_score": _NUM,
        "hype_score": _NUM,
        "works_without_context": {"type": "boolean"},
        "reject_reason": {"type": ["string", "null"]},
    },
    "required": ["funny_score", "hype_score", "works_without_context", "reject_reason"],
    "additionalProperties": False,
}


class JudgeError(RuntimeError):
    pass


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text()


def _client() -> Any:
    import anthropic

    return anthropic.Anthropic()


def _jcfg() -> dict[str, Any]:
    return config.global_config().get("judge", {})


def _call(client: Any, model: str, system: str, content: list[dict], schema: dict, max_tokens: int) -> dict:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    stop = getattr(resp, "stop_reason", None)
    if stop in ("refusal", "max_tokens"):
        raise JudgeError(f"stop_reason={stop}")
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise JudgeError("response is not an object")
    return data


def _transcript(words: list[Word]) -> str:
    return " ".join(w.text for w in words)


def _chat_block(chat_lines: list[str], limit: int = 60) -> str:
    return "\n".join(chat_lines[:limit])


def screen(
    transcript_text: str, chat_lines: list[str], title: str, game: str, client: Any = None
) -> dict[str, Any]:
    """Cheap first pass. Returns funny_score, hype_score, works_without_context, reject_reason."""
    try:
        client = client or _client()
        text = (
            f"Stream title: {title}\nGame: {game}\n\nTranscript:\n{transcript_text}\n\n"
            f"Chat:\n{_chat_block(chat_lines)}"
        )
        data = _call(
            client,
            _jcfg().get("screen_model", ""),
            _prompt("screen.md"),
            [{"type": "text", "text": text}],
            SCREEN_SCHEMA,
            400,
        )
        for k in ("funny_score", "hype_score"):
            data[k] = max(0.0, min(10.0, float(data[k])))
        data["works_without_context"] = bool(data["works_without_context"])
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("screen failed (%s); passing candidate through", e)
        return {"funny_score": 5.0, "hype_score": 5.0, "works_without_context": True, "reject_reason": None}


def _image_block(p: Path) -> dict:
    data = base64.standard_b64encode(Path(p).read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def heuristic_judge(cand: Candidate, words: list[Word], *, game: str | None = None) -> JudgeResult:
    """English fallback copy when the judge cannot run; the detector score stands in for the judge."""
    from . import compact

    score = max(0.0, min(10.0, float(cand.final_score)))
    scfg = config.streamer(cand.stream)
    seed = int(cand.start_s * 1000) ^ sum(map(ord, cand.stream))
    copy = compact.fallback_copy(
        cand.stream, scfg.get("platform", "twitch"), game if game is not None else scfg.get("game"), seed
    )
    t0, t1 = heuristic_trim(cand.peak_s - cand.start_s, cand.duration)
    return JudgeResult(
        funny_score=score,
        hype_score=score,
        visual_context_summary="",
        trim_start_s=t0,
        trim_end_s=t1,
        hook_text=copy["hk"],
        title_options=[copy["tt"]],
        setup_line=copy["ds"].split("\n")[0],
        reject_reason=None,
        source="heuristic",
        description=copy["ds"],
    )


def _validate(data: dict, cand: Candidate) -> JudgeResult:
    dur = cand.duration
    hook_words = str(data["hook_text"]).split()
    if not 2 <= len(hook_words) <= 6:
        raise JudgeError("hook must be 2-6 words")
    titles = [str(t).strip()[:60].rstrip() for t in data["title_options"] if str(t).strip()]
    if not titles:
        raise JudgeError("no titles")
    ts, te = float(data["trim_start_s"]), float(data["trim_end_s"])
    ts, te = max(0.0, ts), min(dur, te)
    if te <= ts:
        raise JudgeError("trim outside window")
    rr = data.get("reject_reason")
    return JudgeResult(
        funny_score=max(0.0, min(10.0, float(data["funny_score"]))),
        hype_score=max(0.0, min(10.0, float(data["hype_score"]))),
        visual_context_summary=str(data["visual_context_summary"]),
        trim_start_s=ts,
        trim_end_s=te,
        hook_text=" ".join(hook_words),
        title_options=titles,
        setup_line=str(data["setup_line"]),
        reject_reason=str(rr) if rr else None,
        source="claude",
    )


def _usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: int(getattr(u, k, 0) or 0) for k in keys}


def call_v2(client: Any, req: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """One standardised judge request -> (raw JSON object, usage). Raises JudgeError on refusal/truncation."""
    resp = client.messages.create(**req)
    stop = getattr(resp, "stop_reason", None)
    if stop in ("refusal", "max_tokens"):
        raise JudgeError(f"stop_reason={stop}")
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise JudgeError("response is not an object")
    return data, _usage(resp)


def judge_clip(
    cand: Candidate,
    words: list[Word],
    chat_lines: list[Any],
    frames: list[tuple[float, Path]],
    *,
    game: str = "",
    platform: str | None = None,
    language: str | None = None,
    task: str = "judge",
    client: Any = None,
    model: str | None = None,
) -> JudgeResult:
    """Standardised multimodal judge (prompts/judge_v2.md + compact JSON). Heuristic fallback on any error."""
    from . import compact

    login = cand.stream
    plat = platform or config.streamer(login).get("platform", "twitch")
    try:
        client = client or _client()
        payload = compact.build_payload(
            streamer=login, platform=plat, game=game, language=language, dur=cand.duration,
            peak_s=cand.peak_s - cand.start_s, transcript=words, chat=chat_lines, task=task,
        )  # fmt: skip
        jcfg = _jcfg()
        mdl = model or (jcfg.get("screen_model") if task == "screen" else jcfg.get("judge_model", ""))
        data, usage = call_v2(client, compact.build_request(mdl, frames, payload))
        out = compact.normalize_output(data, login=login, platform=plat, dur=cand.duration)
        out["t0"], out["t1"] = snap_trim(out["t0"], out["t1"], words, cand.duration)
        if out["pk"] is not None and not out["t0"] <= out["pk"] <= out["t1"]:
            out["pk"] = None
        log.info("judge %s %s sc=%s ok=%s usage=%s", task, login, out["sc"], out["ok"], usage)
        reasons = [k for k, v in out["fl"].items() if v]
        return JudgeResult(
            funny_score=float(out["sc"]),
            hype_score=float(out["sc"]),
            visual_context_summary="",
            trim_start_s=out["t0"],
            trim_end_s=out["t1"],
            peak_rel_s=out["pk"],
            hook_text=out["hk"],
            title_options=[out["tt"]],
            setup_line=out["ds"].split("\n")[0],
            reject_reason=None if out["ok"] else (f"flags: {','.join(reasons)}" if reasons else out["why"]),
            source="claude",
            description=out["ds"],
            flags=out["fl"],
            ok=out["ok"],
            why=out["why"],
            score=out["sc"],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("judge failed (%s); using heuristic", e)
        return heuristic_judge(cand, words, game=game or None)


def judge_candidate(
    cand: Candidate,
    words: list[Word],
    chat_lines: list[str],
    frames: list[Path],
    *,
    title: str = "",
    game: str = "",
    client: Any = None,
    model: str | None = None,
    platform: str | None = None,
    language: str | None = None,
) -> JudgeResult:
    """Backward-compatible entry point; now runs the standardised v2 judge."""
    timed = [
        (f if isinstance(f, tuple) else ((i + 0.5) * cand.duration / max(1, len(frames)), f))
        for i, f in enumerate(frames)
    ]
    return judge_clip(
        cand, words, chat_lines, timed, game=game, platform=platform, language=language, client=client,
        model=model,
    )  # fmt: skip
