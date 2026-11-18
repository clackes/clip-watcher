"""Meme sound effects the judge can put on a beat (owner, 18 Nov: myinstants US trending).

Files live in assets/sfx/<kind>/<name>.mp3, loudness-normalised to -16 LUFS and cut to 3 s. The judge picks
a sound by name for each beat (judge fx[].sx); the renderer places it so its attack lands on the beat.
The "use" lines are repeated in prompts/judge_v2.md (static, cached prompt); a test keeps them in sync.
"""

from __future__ import annotations

from pathlib import Path

from clipwatcher import config

SFX_DIR = config.ROOT / "assets" / "sfx"

# name: (kind folder, attack in seconds = first instant at half the peak level, when to use it)
CATALOG: dict[str, tuple[str, float, str]] = {
    "fahhh": ("fail", 0.25, "a dumb mistake or a loss by a hair, the streamer groans or facepalms"),
    "spongebob-fail": ("fail", 0.03, "a plan that collapses in an embarrassing way"),
    "metal-pipe": ("fail", 0.18, "the impact of a hard crash, fall or sudden death"),
    "bone-crack": ("fail", 0.15, "a painful fall, a wreck, someone gets destroyed"),
    "vine-boom": ("shock", 0.08, "a sudden realisation, a dead stare into the camera, an unhinged line"),
    "among-us-reveal": ("shock", 1.12, "a betrayal or someone exposed, a sus reveal"),
    "dexter": ("shock", 0.03, "an ominous twist, the 'he knows' moment"),
    "shocked": ("shock", 0.10, "a jump scare or a jaw-drop surprise"),
    "anime-wow": ("hype", 0.20, "an insane play, a clutch, something genuinely impressive"),
    "screaming-clapping": ("hype", 0.00, "a win, a goal, a huge clutch the room celebrates"),
    "capcut-explosion": ("hype", 0.05, "something blows up or a massive hit lands"),
    "bruh": ("funny", 0.12, "a stupid line or decision, deadpan disbelief"),
    "cat-laugh": ("funny", 0.30, "someone gets roasted or embarrassed"),
    "mj-hee-hee": ("funny", 0.53, "a cheeky, smug or awkward moment"),
    "rizz": ("funny", 0.23, "flirting, a smooth line, a confident flex"),
}


def path(name: str) -> Path | None:
    """File of a catalogued sound, None if unknown or missing on disk."""
    if name not in CATALOG:
        return None
    p = SFX_DIR / CATALOG[name][0] / f"{name}.mp3"
    return p if p.exists() else None


def attack(name: str) -> float:
    return CATALOG[name][1] if name in CATALOG else 0.0
