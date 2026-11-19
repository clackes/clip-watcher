"""Shared data types. Every module talks through these; keep them stable."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ChatMessage:
    """One chat line in the unified format (JSONL, one object per line)."""

    ts_ms: int  # server timestamp (tmi-sent-ts / Kick created_at) in ms; for VODs: ms from VOD start
    user: str  # login, lowercase
    text: str
    platform: str = "twitch"  # twitch | kick
    is_bot: bool = False
    is_notice: bool = False  # sub / raid / gift notice (USERNOTICE)
    notice_type: str | None = None  # "raid", "subgift", "submysterygift", "sub", ...
    viewers: int | None = None  # raid size etc. when known

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    """A moment found by the detector. Times are seconds from the start of the source (VOD/recording)."""

    stream: str  # streamer login
    peak_s: float  # when chat score first crossed the pre-threshold, minus channel delay
    start_s: float  # draft window start (peak - before)
    end_s: float  # draft window end (peak + after)
    chat_score: float
    final_score: float
    x_normal: float = 0.0  # baseline "x normal" ratio (Chat Clip Finder)
    unique_chatters: int = 0
    signals: dict[str, float] = field(default_factory=dict)
    note: str = ""  # operator's request when a clip is redone from Telegram, passed to the judge
    tag: str = ""  # clip id suffix of a redo ("-r1")

    @property
    def duration(self) -> float:
        return self.end_s - self.start_s


@dataclass
class JudgeResult:
    funny_score: float
    hype_score: float
    visual_context_summary: str
    trim_start_s: float  # relative to the candidate window start
    trim_end_s: float
    hook_text: str  # meme caption for the post, not drawn on the video (judge v2: sentence + emoji)
    title_options: list[str]
    setup_line: str
    reject_reason: str | None = None
    source: str = "claude"  # claude | heuristic
    description: str = ""  # ready-to-post description (teaser + credit, CTA, hashtags)
    flags: dict[str, bool] = field(default_factory=dict)  # sp spoiler, mu music risk, gb gambling
    peak_rel_s: float | None = None  # strongest reaction, relative to the candidate window (cold open)
    ok: bool = True  # publishable per the judge
    why: str = ""  # one line for the Telegram approval card
    score: int = 0  # 1-10 viral score
    cam_xy: tuple[float, float] | None = None  # judge: centre (0-1) of the cam of whoever makes the moment
    # editor beats: (t relative to the candidate window, kind hype|fail|shock|funny, label like *FAIL*,
    # meme sound name from clipwatcher.sfx or "")
    beats: list[tuple[float, str, str, str]] = field(default_factory=list)
    # action labels for stretches nobody talks in: (start, end, "*WATCHING*"), relative to the window
    actions: list[tuple[float, float, str]] = field(default_factory=list)


@dataclass
class Word:
    """Word-level transcript token, times relative to the clip start."""

    start: float
    end: float
    text: str
