"""Synthetic chat generator: background chat + planted spikes, a raid burst and a giveaway burst."""

from __future__ import annotations

import json
import random
from pathlib import Path

from clipwatcher.models import ChatMessage

SPIKES = [400, 1000, 1700]  # planted funny moments (chat reacts ~8 s later)
RAID_AT = 1300
GIVEAWAY_AT = 2000
DURATION = 2400
REACTION_DELAY = 8

_BG = [
    "nice", "ok", "gg", "hello", "what game", "wow", "hi everyone", "yes", "no way",
    "nice one", "come on", "how are you", "just watching", "great", "interesting", "idk",
    "play the other route", "good stream", "well played", "ugh", "where are you at",
]  # fmt: skip


def make_chat(seed: int = 7) -> list[ChatMessage]:
    rng = random.Random(seed)
    users = [f"user{i}" for i in range(60)]
    msgs: list[ChatMessage] = []

    def add(t: float, user: str, text: str, **kw):
        msgs.append(ChatMessage(ts_ms=int(t * 1000), user=user, text=text, **kw))

    t = 0.0
    while t < DURATION:
        t += rng.expovariate(0.8)
        txt = rng.choice(_BG)
        if rng.random() < 0.03:
            txt = "KEKW"
        add(t, rng.choice(users), txt)
    for m in SPIKES:
        for u in rng.sample(users, 32):
            add(
                m + REACTION_DELAY + rng.uniform(0, 8),
                u,
                rng.choice(["KEKW", "LMAO", "clip it", "AHAHAHAH", "KEKW KEKW"]),
            )
    # raid burst: notice + 200 messages from brand new users
    add(RAID_AT, "raider", "", is_notice=True, notice_type="raid", viewers=300)
    for i in range(200):
        add(RAID_AT + 1 + rng.uniform(0, 15), f"newbie{i % 80}", "raid hype")
    # giveaway: bot message then most users type the same word
    add(GIVEAWAY_AT, "nightbot", "Giveaway! type RAFFLE to enter", is_bot=True)
    for u in rng.sample(users, 48):
        add(GIVEAWAY_AT + 1 + rng.uniform(0, 10), u, "RAFFLE")
    msgs.sort(key=lambda m: m.ts_ms)
    return msgs


def to_tdl_json(msgs: list[ChatMessage], path: Path) -> Path:
    comments = []
    for m in msgs:
        msg: dict = {"body": m.text, "fragments": [{"text": m.text, "emoticon": None}]}
        if m.is_notice:
            msg["user_notice_params"] = {
                "msg-id": m.notice_type,
                "msg-param-viewerCount": str(m.viewers or 0),
            }
        comments.append(
            {
                "content_offset_seconds": m.ts_ms / 1000,
                "commenter": {"name": m.user, "display_name": m.user.title()},
                "message": msg,
            }
        )
    path.write_text(json.dumps({"comments": comments}))
    return path
