"""Learn which chat words mark a real moment, per kind of stream and per streamer (D16).

    python scripts/learn_keywords.py collect --category "Grand Theft Auto VI" [--channels 40] [--hours 10]
    python scripts/learn_keywords.py collect --streamers zackrawrr,clix          # your own, any category
    python scripts/learn_keywords.py learn   --category "Grand Theft Auto VI" [--validate] [--write]

collect: the biggest live channels of a category (in the channel's language) and their broadcasts of the last
days, cut to the stretches Twitch marks as that category, chat read from the replay, plus each channel's most
viewed viewer-made clips. Everything lands in work/corpus/<category>/<login>/ and is only downloaded once;
run it again to continue or to add newer broadcasts and clips. Twitch only, no login needed.

learn: a channel's moments are what its viewers clipped. A word (or a pair of words) is a keyword when
chatters type it far more during moments than in the rest of the chat, moment after moment: across many
channels it is the category's, in one channel it is that streamer's (its emotes, its in-jokes). A word of a
single moment is what that moment was about and is left out. --validate runs the real detector with and
without the result and counts the viewer clips it finds, on clips no word was learned from. --write saves to
config/keywords.yaml, which the detector reads, only what found more clips in that test.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from clipwatcher import config
from clipwatcher.chat import twitch_replay
from clipwatcher.chat.convert import write_jsonl

CORPUS = config.ROOT / "work" / "corpus"
OWN = "_streamers"  # the project's own streamers, whatever they streamed (collect --streamers)
_STREAMS_Q = (
    "query($n: String!) { game(name: $n) { name streams(first: 100) { edges { node { viewersCount language"
    " broadcaster { login } } } } } }"
)
_VIDEOS_Q = (
    "query($login: String!) { user(login: $login) { videos(first: 10, type: ARCHIVE, sort: TIME) { edges {"
    " node { id createdAt lengthSeconds game { name } moments(momentRequestType: VIDEO_CHAPTER_MARKERS,"
    " first: 40) { edges { node { positionMilliseconds durationMilliseconds details {"
    " ... on GameChangeMomentDetails { game { name } } } } } } } } } } }"
)
_CLIPS_Q = (
    "query($login: String!, $period: ClipsPeriod!) { user(login: $login) { clips(first: 100, criteria:"
    " {period: $period, sort: VIEWS_DESC}) { edges { node { slug title viewCount videoOffsetSeconds"
    " durationSeconds video { id } } } } } }"
)


def slug(category: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def category_channels(category: str, n: int, language: str) -> tuple[str, list[str]]:
    """(the category's name as Twitch writes it, logins of its biggest live channels in ``language``)."""
    d = twitch_replay._post({"query": _STREAMS_Q, "variables": {"n": category}})
    game = (d.get("data") or {}).get("game")
    if not game:
        raise SystemExit(f"Twitch knows no category {category!r}")
    rows = [e["node"] for e in game["streams"]["edges"]]
    rows = [r for r in rows if (r.get("language") or "").lower() == language.lower()]
    rows.sort(key=lambda r: -r["viewersCount"])
    return game["name"], [r["broadcaster"]["login"].lower() for r in rows[:n]]


def category_segments(login: str, category: str | None, since_ms: int) -> list[dict]:
    """Stretches of this channel's recent broadcasts that were in the category, newest first; with no
    category every recent broadcast, whole."""
    d = twitch_replay._post({"query": _VIDEOS_Q, "variables": {"login": login}})
    out = []
    for e in (((d.get("data") or {}).get("user") or {}).get("videos") or {}).get("edges") or []:
        v = e["node"]
        created, length = _ms(v["createdAt"]), int(v["lengthSeconds"])
        if created + length * 1000 < since_ms:
            continue
        spans = []
        chapters = [m["node"] for m in (v.get("moments") or {}).get("edges") or []]
        if category is None:
            spans.append((0, length))
        for c in chapters if category else ():
            if (((c.get("details") or {}).get("game") or {}).get("name") or "").lower() != category.lower():
                continue
            a = c["positionMilliseconds"] // 1000
            b = a + c["durationMilliseconds"] // 1000 if c["durationMilliseconds"] else length  # 0: still on
            spans.append((a, min(b, length)))
        if (
            category
            and not chapters
            and ((v.get("game") or {}).get("name") or "").lower() == category.lower()
        ):
            spans.append((0, length))
        out += [
            {
                "vod": v["id"],
                "created_ms": created,
                "start_s": a,
                "end_s": b,
                "category": category is not None,
            }
            for a, b in spans
            if b - a > 600
        ]
    return out


def viewer_clips(login: str, known: list[dict]) -> list[dict]:
    """The channel's most viewed viewer-made clips of the last day, week and month (100 each is all Twitch
    shows without a login), added to the ones already known."""
    by_key = {
        c.get("slug") or (c.get("video") or {}).get("id", "") + f"@{c.get('videoOffsetSeconds')}": c
        for c in known
    }
    for period in ("LAST_DAY", "LAST_WEEK", "LAST_MONTH"):
        try:
            c = twitch_replay._post({"query": _CLIPS_Q, "variables": {"login": login, "period": period}})
        except Exception:  # noqa: BLE001 - clips are what there is; the chat is still worth having
            continue
        for e in (((c.get("data") or {}).get("user") or {}).get("clips") or {}).get("edges") or []:
            n = e["node"]
            old_key = ((n.get("video") or {}).get("id") or "") + f"@{n.get('videoOffsetSeconds')}"
            by_key.pop(old_key, None)  # saved before clips carried their slug
            by_key[n["slug"]] = n
    return list(by_key.values())


def collect(a: argparse.Namespace) -> int:
    deadline = time.time() + a.max_minutes * 60
    extra = [x.strip().lower() for x in (a.also or "").split(",") if x.strip()]
    if a.category:
        category, logins = category_channels(a.category, a.channels, a.language)
        root = CORPUS / slug(category)
        had = sorted(x.name for x in root.iterdir() if x.is_dir()) if root.exists() else []
        logins = list(dict.fromkeys(logins + extra + had))  # channels collected before get their newer clips
    elif a.streamers:
        category, root = None, CORPUS / OWN
        logins = [x.strip().lower() for x in a.streamers.split(",") if x.strip()]
    else:
        raise SystemExit("collect needs --category or --streamers")
    since_ms = int((time.time() - a.days * 86400) * 1000)
    print(f"{category or 'own streamers'}: {len(logins)} channels -> {root}")
    todo = 0
    part, parts = (int(x) for x in a.shard.split("/"))
    for login in logins[part::parts]:  # --shard 1/3: every third channel, so three runs can share the work
        d = root / login
        d.mkdir(parents=True, exist_ok=True)
        try:
            segs = category_segments(login, category, since_ms)
        except Exception as e:  # noqa: BLE001 - one channel that cannot be listed does not stop the rest
            print(f"  {login}: not listed ({e})")
            continue
        budget, picked = a.hours * 3600, []
        for s in segs:  # newest first, up to --hours per channel
            if budget <= 0:
                break
            s["end_s"] = min(s["end_s"], s["start_s"] + int(budget))
            budget -= s["end_s"] - s["start_s"]
            picked.append(s)
        seg_file, clip_file = d / "segments.json", d / "clips.json"
        listed = json.loads(seg_file.read_text()) if seg_file.exists() else []
        names = {f"{s['vod']}_{s['start_s']}_{s['end_s']}" for s in listed}
        listed += [s for s in picked if f"{s['vod']}_{s['start_s']}_{s['end_s']}" not in names]
        seg_file.write_text(
            json.dumps(listed)
        )  # every stretch ever picked: saved chats keep their start time
        clip_file.write_text(
            json.dumps(viewer_clips(login, json.loads(clip_file.read_text()) if clip_file.exists() else []))
        )
        for s in picked:
            out = d / f"{s['vod']}_{s['start_s']}_{s['end_s']}.jsonl"
            if out.exists() or list(d.glob(f"{s['vod']}_{s['start_s']}_*.jsonl")):
                continue  # read already (under another end, if the broadcast was still running then)
            if time.time() > deadline:
                todo += 1
                continue
            t0 = time.time()
            msgs = twitch_replay.fetch(s["vod"], s["start_s"], s["end_s"], workers=8)
            tmp = out.with_suffix(".part")
            write_jsonl(msgs, tmp)
            tmp.replace(out)
            hours = (s["end_s"] - s["start_s"]) / 3600
            print(f"  {login}: {hours:.1f} h, {len(msgs)} messages in {time.time() - t0:.0f} s", flush=True)
    print("all downloaded" if not todo else f"{todo} stretches still to download: run collect again")
    return 0 if not todo else 3


# ------------------------------------------------------------------------------------------- learn
_EDGES = "\"'()[]{}<>.,;:*~_`|/\\-+=^"  # stripped from the ends of a word
# padding against the duplicate-message filter, emoji variation marks, tag characters
_INVISIBLE = re.compile(
    "["
    + "".join(map(chr, (0x34F, 0x2060, 0xFE0E, 0xFE0F)))
    + f"{chr(0x200B)}-{chr(0x200F)}"
    + f"{chr(0xE0000)}-{chr(0xE007F)}]"
)
_RUN3 = re.compile(r"(.)\1{2,}")
_ENGLISH = frozenset("the is you it he and this that of for what his are was with just".split())
STATS_VERSION = 8


def raw_tokens(text: str) -> list[str]:
    """Chat words as typed, lower case. Links, mentions and commands are not vocabulary."""
    out = []
    for w in _INVISIBLE.sub("", text).lower().split():
        if w.startswith(("http", "@")) or (w[0] == "!" and w[1:2].isalnum()):
            continue
        w = w.strip(_EDGES)
        if any(c.isalnum() for c in w):
            w = w.rstrip("?!")  # "what?!" is the word what; "???" on its own stays a reaction
        if w and len(w) <= 24:
            out.append(w)
    return out


def fold(word: str, known: dict[str, int]) -> str:
    """A word with held keys back to how it is normally typed: lmaoooo -> lmao, killl -> kill, wwww -> w.
    Whether a held letter stands for one or two is decided by which spelling this chat types more often."""
    if len(set(word)) == 1 and len(word) > 1:
        return word[0]  # ww, lll, ???: one reaction, however long
    while True:
        m = _RUN3.search(word)
        if not m:
            return word
        one = word[: m.start()] + m.group(1) + word[m.end() :]
        two = word[: m.start()] + m.group(1) * 2 + word[m.end() :]
        word = two if known.get(two, 0) > known.get(one, 0) else one


def features(text: str, known: dict[str, int]) -> set[str]:
    """What a message says, as the things a keyword list can hold: its words, and its pairs of words."""
    toks = [fold(w, known) for w in raw_tokens(text)]
    out = set(toks)
    if 2 <= len(toks) <= 6:
        out.update(f"{a} {b}" for a, b in zip(toks, toks[1:], strict=False) if a != b)
    return out


def clip_zones(seg: dict, clips: list[dict]) -> list[tuple[float, float]]:
    """Wall-clock (start, end) of the moments of one stretch that viewers clipped: from the clip's first
    second to 20 s after its last, the time in which chat reacts to what is in it. Clips of the same moment
    are one zone."""
    t0 = seg["created_ms"] / 1000
    spans = sorted(
        (t0 + c["videoOffsetSeconds"], t0 + c["videoOffsetSeconds"] + c["durationSeconds"] + 20)
        for c in clips
        if (c.get("video") or {}).get("id") == seg["vod"]
        and c.get("videoOffsetSeconds") is not None
        and seg["start_s"] + 60 <= c["videoOffsetSeconds"] <= seg["end_s"] - 60
    )
    zones: list[list[float]] = []
    for a, b in spans:
        if zones and a <= zones[-1][1]:
            zones[-1][1] = max(zones[-1][1], b)
        else:
            zones.append([a, b])
    return [(a, b) for a, b in zones]


def stretches(d: Path) -> list[tuple[Path, dict]]:
    """(chat file, its stretch) of one channel; a stretch downloaded twice while it grew counts once."""
    segs = {
        f"{s['vod']}_{s['start_s']}_{s['end_s']}": s for s in json.loads((d / "segments.json").read_text())
    }
    best: dict[tuple[str, int], tuple[Path, dict]] = {}
    for f in sorted(d.glob("*.jsonl")):
        seg = segs.get(f.stem)
        if seg is None:  # listed under another end: the broadcast was still running when it was read
            vod, a, b = f.stem.split("_")
            same = [s for s in segs.values() if s["vod"] == vod]
            if not same:
                continue
            seg = {**same[0], "start_s": int(a), "end_s": int(b)}
        key = (seg["vod"], seg["start_s"])
        if key not in best or seg["end_s"] > best[key][1]["end_s"]:
            best[key] = (f, seg)
    return list(best.values())


@contextmanager
def keyword_list(keywords: dict[str, float]) -> Iterator[None]:
    """Run the detector with exactly these keywords, whatever the configuration says."""
    configured = config.keywords_for
    config.keywords_for = lambda name=None, category=None: keywords
    try:
        yield
    finally:
        config.keywords_for = configured


def detector_moments(msgs: list, login: str, per_hour: float) -> list[tuple[float, float]]:
    """(start, end) of the chat reactions the detector would clip today, with the global keywords only: the
    seconds from each spike on in which chat is about it."""
    from clipwatcher.detect import detector

    base = dict(config.global_config().get("detector", {}).get("keywords", {}))
    top = max(1, round(per_hour * (msgs[-1].ts_ms - msgs[0].ts_ms) / 3.6e6))
    with keyword_list(base):
        return [(c.peak_s, c.peak_s + 30) for c in detector.detect(msgs, streamer=login, top=top)]


def channel_stats(d: Path, per_hour: float = 0.0) -> dict | None:
    """One channel's numbers. Its moments are what viewers clipped (with ``per_hour`` also what the
    detector would clip today with the global keywords, that many an hour). Per word (and per pair of
    words): the chatters who typed it during a moment, the messages holding it in the rest of the chat, in
    how many moments it was up (twice its usual share of the chat or more), the most chatters it had in one
    moment; then the first, third and fourth again without the moments around every second viewer clip
    (those are kept to test the result on). Cached until the channel's files change."""
    from bisect import bisect_right
    from collections import Counter, defaultdict

    from clipwatcher.chat.convert import read_jsonl

    if not (d / "segments.json").exists():
        return None
    files = stretches(d)
    if not files:
        return None
    clips_file = d / "clips.json"
    stamp = [[f.name, f.stat().st_size] for f, _ in files]
    stamp.append(["clips", clips_file.stat().st_size if clips_file.exists() else 0])
    cache = d / f"stats_{per_hour:g}.json"
    if cache.exists():
        got = json.loads(cache.read_text())
        if got.get("stamp") == stamp and got.get("version") == STATS_VERSION:
            return got
    bots = {b.lower() for b in config.global_config().get("detector", {}).get("bots", [])}
    clips = json.loads(clips_file.read_text()) if clips_file.exists() else []
    base: Counter[str] = Counter()
    moments: list[tuple[bool, int, dict[str, int]]] = []  # (in the learning half, chatters, word -> chatters)
    known_all: Counter[str] = Counter()
    n_b = n_msgs = n_clip = n_spike = in_category = 0
    seconds = 0.0
    for f, seg in files:
        every = read_jsonl(f)
        msgs = [m for m in every if not (m.is_bot or m.user in bots or m.is_notice or m.text.startswith("!"))]
        if len(msgs) < 500:
            continue
        known: Counter[str] = Counter()
        for m in msgs:
            known.update(raw_tokens(m.text))
        known_all.update(known)
        zones = clip_zones(seg, clips)
        spikes = detector_moments(every, d.name, per_hour) if per_hour > 0 else []
        marked = [(a, b, (n_clip + i) % 2 == 0, True) for i, (a, b) in enumerate(zones)]
        marked += [(a, b, True, False) for a, b in spikes]
        n_clip += len(zones)
        n_spike += len(spikes)
        merged: list[list] = []  # start, end, in the learning half
        for a, b, learn_, _clip in sorted(marked):
            if merged and a <= merged[-1][1] + 20:  # the same moment, or its tail: not ordinary chat either
                merged[-1][1] = max(merged[-1][1], b)
                merged[-1][2] = merged[-1][2] and learn_
            else:
                merged.append([a, b, learn_])
        starts = [m[0] for m in merged]
        used: list[dict[str, set[str]]] = [defaultdict(set) for _ in merged]
        users: list[set[str]] = [set() for _ in merged]
        for m in msgs:
            t = m.ts_ms / 1000
            i = bisect_right(starts, t + 10) - 1
            if i >= 0 and t <= merged[i][1] + 15:  # 10 s before a moment to 15 s after: not ordinary chat
                if merged[i][0] <= t <= merged[i][1]:
                    users[i].add(m.user)
                    for x in features(m.text, known):
                        used[i][x].add(m.user)
                continue
            n_b += 1
            base.update(features(m.text, known))
        for i, who in enumerate(users):
            if len(who) >= 8:
                moments.append((merged[i][2], len(who), {x: len(us) for x, us in used[i].items()}))
        n_msgs += len(msgs)
        seconds += (msgs[-1].ts_ms - msgs[0].ts_ms) / 1000
        in_category += len(msgs) if seg.get("category", True) else 0
    tok: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0])
    n_a = n_ae = 0
    for learn_, who, used_ in moments:
        n_a += who
        n_ae += who if learn_ else 0
        for x, n in used_.items():
            row = tok[x]
            up = n >= 2 and n / who >= 2 * (base[x] + 0.5) / (n_b + 1)
            row[0] += n
            row[2] += up
            row[3] = max(row[3], n)
            if learn_:
                row[4] += n
                row[5] += up
                row[6] = max(row[6], n)
    for x, row in tok.items():
        row[1] = base[x]
    total = sum(known_all.values()) or 1
    out = {
        "version": STATS_VERSION, "stamp": stamp, "login": d.name, "hours": round(seconds / 3600, 2),
        "messages": n_msgs, "clipped": n_clip, "spikes": n_spike, "moments": len(moments),
        "moments_even": sum(1 for m in moments if m[0]), "chatters": n_a, "chatters_even": n_ae,
        "base_messages": n_b, "english": round(sum(known_all[w] for w in _ENGLISH) / total, 4),
        "in_category": round(in_category / max(1, n_msgs), 3),
        "tok": {x: r for x, r in tok.items() if r[0] >= 2},
    }  # fmt: skip
    cache.write_text(json.dumps(out))
    return out


def lift(a: float, n_a: float, b: float, n_b: float) -> float:
    """How many times more often a chatter types the word during a moment than an ordinary message holds
    it."""
    return ((a + 0.5) / (n_a + 1)) / ((b + 0.5) / (n_b + 1))


def weight(lift_: float) -> float:
    """Keyword weight from its lift, on the scale of the hand-made list (1 = mild, 3 = "clip it"):
    twice as common during a moment 1.5, four times 2, eight times 2.5, sixteen times 3."""
    import math

    return max(1.0, min(3.0, round((1 + math.log2(max(lift_, 1.0)) / 2) * 2) / 2))


def _cols(part: str) -> tuple[int, int, int, str, str]:
    """Where the numbers of all moments ("all") or of the learning half ("even") are in a word's row."""
    if part == "all":
        return 0, 2, 3, "chatters", "moments"
    return 4, 5, 6, "chatters_even", "moments_even"


def category_words(stats: list[dict], a: argparse.Namespace, part: str = "all") -> dict[str, dict]:
    """Words that mark a moment in this kind of stream: typed far more during moments than in the rest of
    the chat, up in many moments, and so in many channels."""
    import math

    ia, im, it, na_key, m_key = _cols(part)
    chans = [s for s in stats if s[m_key] >= a.min_moments_channel and s["english"] >= a.min_english]
    if not chans:
        return {}
    need = max(a.min_channels, math.ceil(a.channel_share * len(chans)))
    n_a = sum(s[na_key] for s in chans)
    n_b = sum(s["base_messages"] for s in chans)
    acc: dict[
        str, list[float]
    ] = {}  # word -> [chatters, base messages, moments, most in one, channels up, seen]
    for s in chans:
        for x, r in s["tok"].items():
            row = acc.setdefault(x, [0, 0, 0, 0, 0, 0])
            row[0] += r[ia]
            row[1] += r[1]
            row[2] += r[im]
            row[3] = max(row[3], r[it])
            if r[ia] >= 5:
                row[5] += 1
                row[4] += lift(r[ia], s[na_key], r[1], s["base_messages"]) >= a.channel_lift
    floor = max(a.min_moments, a.moment_share * sum(s[m_key] for s in chans))
    out = {}
    for x, (ca, cb, moments, top, up, seen) in acc.items():
        if ca < 30 or up < need or up < 0.6 * seen or moments < floor or top > a.max_one_moment * ca:
            continue
        lf = lift(ca, n_a, cb, n_b)
        z = math.log(lf) / math.sqrt(1 / (ca + 0.5) + 1 / (cb + 0.5))
        if lf >= a.category_lift and z >= 4:
            out[x] = {
                "weight": weight(lf),
                "lift": round(lf, 1),
                "channels": int(up),
                "moments": int(moments),
            }
    return drop_covered(out)


def plain_words(stats: list[dict], a: argparse.Namespace) -> set[str]:
    """Words many chats use during moments without them marking anything there (my, why, this is): seen in
    five channels or more and, in the typical one, hardly more common during a moment than otherwise."""
    import statistics

    seen: dict[str, list[float]] = {}
    for s in stats:
        if s["moments"] >= a.min_moments_channel:
            for x, r in s["tok"].items():
                if r[0] >= 5:
                    seen.setdefault(x, []).append(lift(r[0], s["chatters"], r[1], s["base_messages"]))
    return {x for x, lifts in seen.items() if len(lifts) >= 5 and statistics.median(lifts) < a.plain_lift}


def streamer_words(
    s: dict, known: dict[str, dict], plain: set[str], a: argparse.Namespace, part: str = "all"
) -> dict[str, dict]:
    """Words that mark a moment in this channel's chat and are not the category's: its emotes, its
    in-jokes, the reactions its viewers prefer. They have to come back in many moments (a word of one
    moment is what that moment was about) and must not be plain talk in everybody else's chat."""
    import math

    ia, im, it, na_key, m_key = _cols(part)
    if s[m_key] < a.streamer_moments_channel:
        return {}
    need = max(a.streamer_moments, a.streamer_share * s[m_key])
    out = {}
    for x, r in s["tok"].items():
        if x in plain or r[ia] < 20 or r[im] < need or r[it] > a.streamer_one_moment * r[ia]:
            continue
        lf = lift(r[ia], s[na_key], r[1], s["base_messages"])
        z = math.log(lf) / math.sqrt(1 / (r[ia] + 0.5) + 1 / (r[1] + 0.5))
        if lf >= a.streamer_lift and z >= 5:
            out[x] = {"weight": min(2.5, weight(lf)), "lift": round(lf, 1), "moments": r[im]}
    out = drop_covered(out, known)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["moments"])[: a.streamer_max])


def drop_covered(words: dict[str, dict], known: dict[str, dict] | None = None) -> dict[str, dict]:
    """Without the words another word already matches as held keys (gyatt is gyat) and without the pairs
    whose own word is kept at the same weight ("she crazy" next to crazy). ``known``: words kept elsewhere
    (the category's, when choosing a streamer's)."""
    from clipwatcher.detect.detector import _held_keys

    kept: dict[str, dict] = dict(known or {})
    pats: list[re.Pattern[str]] = [re.compile(_held_keys(k), re.IGNORECASE) for k in kept]
    for x, info in sorted(words.items(), key=lambda kv: (" " in kv[0], -kv[1]["moments"], len(kv[0]))):
        if x in kept or any(p.fullmatch(x) for p in pats):
            continue
        if " " in x and any(kept.get(w, {}).get("weight", 0) >= info["weight"] for w in x.split()):
            continue
        kept[x] = info
        pats.append(re.compile(_held_keys(x), re.IGNORECASE))
    return {x: i for x, i in kept.items() if x not in (known or {})}


def global_weight(word: str) -> float:
    """The weight the global configuration already gives a message that is just this word: its keyword
    list (held keys included) and the laughter pattern. 0 when it knows nothing of it."""
    from clipwatcher.detect.detector import _kw_patterns

    cfg = config.global_config().get("detector", {})
    got = [w for _, pat, w in _kw_patterns(dict(cfg.get("keywords", {}))) if pat.fullmatch(word)]
    if cfg.get("laughter_regex") and re.fullmatch(cfg["laughter_regex"], word):
        got.append(2.0)
    return max(got, default=0.0)


def _map(fn, jobs: list, workers: int) -> list:
    """fn over jobs, in ``workers`` processes (1: here, in order)."""
    from concurrent.futures import ProcessPoolExecutor

    if workers <= 1 or len(jobs) <= 1:
        return [fn(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, jobs))


def _stats_job(job: tuple[Path, float]) -> dict | None:
    return channel_stats(*job)


def load_stats(category: str, per_hour: float = 0.0, workers: int = 6) -> tuple[list[dict], list[dict]]:
    """(channels of the category, the project's own streamers collected whatever they streamed)."""

    def dirs(root: Path) -> list[Path]:
        return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []

    cat, own = dirs(CORPUS / slug(category)), dirs(CORPUS / OWN)
    got = _map(_stats_job, [(d, per_hour) for d in cat + own], workers)
    return [s for s in got[: len(cat)] if s], [s for s in got[len(cat) :] if s]


def learn(a: argparse.Namespace) -> int:
    stats, own = load_stats(a.category, a.detector_moments, a.jobs)
    if not stats:
        raise SystemExit(f"no chat in {CORPUS / slug(a.category)}: run collect first")
    used = [s for s in stats if s["moments"] >= a.min_moments_channel and s["english"] >= a.min_english]
    hours, msgs = sum(s["hours"] for s in stats), sum(s["messages"] for s in stats)
    print(f"{a.category}: {len(stats)} channels, {hours:.0f} h of chat, {msgs} messages")
    clipped, spikes = sum(s["clipped"] for s in used), sum(s["spikes"] for s in used)
    also = f" and {spikes} moments the detector clips today" if spikes else ""
    print(
        f"{sum(s['moments'] for s in used)} moments with a reaction in chat, from {clipped} viewer clips"
        f"{also}, in the {len(used)} channels that chat in English"
    )
    left = [
        f"{s['login']} ({'not English' if s['english'] < a.min_english else str(s['moments']) + ' moments'})"
        for s in stats
        if s not in used
    ]
    if left:
        print("left out: " + ", ".join(left))
    category = category_words(stats, a)
    for x, i in category.items():
        i["global"] = global_weight(x)
    new = sum(1 for i in category.values() if i["weight"] > i["global"])
    print(
        f"\ncategory keywords ({len(category)}, {new} of them new or stronger than the global list has them)"
    )
    print(f"  {'word':20} {'weight':>6} {'global':>6} {'lift':>6} {'channels':>8} {'moments':>8}")
    for x, i in sorted(category.items(), key=lambda kv: -kv[1]["moments"]):
        was = f"{i['global']:6.1f}" if i["global"] else "     -"
        print(f"  {x:20} {i['weight']:6.1f} {was} {i['lift']:6.1f} {i['channels']:8d} {i['moments']:8d}")
    streamers: dict[str, dict] = {}
    plain = plain_words(stats + own, a)
    for s in stats + own:
        words = streamer_words(s, category, plain, a)
        words = {x: i for x, i in words.items() if i["weight"] > global_weight(x)}
        if words:
            streamers[s["login"]] = {**streamers.get(s["login"], {}), **words}
    print(f"\nper-streamer keywords ({len(streamers)} channels)")
    for login, words in sorted(streamers.items()):
        print(f"  {login}: " + ", ".join(f"{x} ({i['weight']:g})" for x, i in words.items()))
    tested = validate(stats, own, a) if a.validate or a.write else None
    if a.write:
        # Only what finds more viewer clips than the detector does without it is written (--force: anyway)
        need = max(a.min_gain_clips, a.min_gain * tested["global"])
        if tested["category"] - tested["global"] < need and not a.force:
            print(
                f"\ncategory keywords not written: with them the detector found {tested['category']} of "
                f"{tested['clips']} viewer clips, without them {tested['global']} (asked: {need:.0f} more)"
            )
            category = {}
        if tested["streamer"] - tested["without_streamer"] < need and not a.force:
            print(
                f"per-streamer keywords not written: with them {tested['streamer']} viewer clips found, "
                f"without them {tested['without_streamer']}"
            )
            streamers = {}
        category = {x: i for x, i in category.items() if i["weight"] > i["global"]}
        named = {x.strip().lower() for x in (a.streamers or "").split(",") if x.strip()}
        only = None if named == {"all"} else named or {k.lower() for k in config.streamers()}
        streamers = {k: v for k, v in streamers.items() if only is None or k in only}
        if category or streamers:
            write_keywords(a.category, category, streamers, used)
    return 0


def _hits(job: tuple) -> tuple[str, int, list[int]]:
    """(channel, clipped moments in the half kept for testing, how many of them each keyword list finds) for
    one channel: the batch detector over its chat, the same number of clips an hour for every list."""
    from clipwatcher.chat.convert import read_jsonl
    from clipwatcher.detect import detector

    d, lists, per_hour = job
    clips = json.loads((d / "clips.json").read_text())
    found = [0] * len(lists)
    total = n = 0
    for f, seg in stretches(d):
        msgs = read_jsonl(f)
        if len(msgs) < 200:
            continue
        zones = clip_zones(seg, clips)  # the same order as channel_stats: every second one is for testing
        test = [z for i, z in enumerate(zones) if (n + i) % 2 == 1]
        n += len(zones)
        total += len(test)
        top = max(1, round(per_hour * (msgs[-1].ts_ms - msgs[0].ts_ms) / 3.6e6))
        for i, kw in enumerate(lists):
            with keyword_list(kw):
                cands = detector.detect(msgs, streamer=d.name, top=top)
            fires = [c.signals.get("fire_s", c.peak_s + 8) for c in cands]
            found[i] += sum(1 for a, b in test if any(a - 5 <= t <= b for t in fires))
    return d.name, total, found


def validate(stats: list[dict], own: list[dict], a: argparse.Namespace) -> dict[str, int]:
    """Does the result find more of what viewers clipped? Every channel is tested on the half of its viewer
    clips that no word was learned from, with category words learned from the other channels only. Our own
    streamers (collected whatever they streamed) are tested with their own words alone. Returns the
    counts: clips, found with the global list, with the category words too, with the streamer's too."""
    base = dict(config.global_config().get("detector", {}).get("keywords", {}))
    chans = [s for s in stats if s["clipped"] >= 6 and s["english"] >= a.min_english]
    plain = plain_words(stats + own, a)
    cap = a.max_weight

    def added(words: dict[str, dict]) -> dict[str, float]:
        return {
            x: min(cap, i["weight"]) for x, i in words.items() if min(cap, i["weight"]) > global_weight(x)
        }

    jobs = []
    for n, s in enumerate(chans):
        others = [o for m, o in enumerate(chans) if m % a.folds != n % a.folds]
        cat = category_words(stats if a.ceiling else others, a)
        mine = streamer_words(s, cat, plain, a, part="all" if a.ceiling else "even")
        cat_kw = {**base, **added(cat)}
        jobs.append(
            (CORPUS / slug(a.category) / s["login"], [base, cat_kw, {**cat_kw, **added(mine)}], a.per_hour)
        )
    every = category_words(stats, a)
    for s in own:
        if s["clipped"] >= 6:
            mine = streamer_words(s, every, plain, a, part="even")
            jobs.append((CORPUS / OWN / s["login"], [base, base, {**base, **added(mine)}], a.per_hour))
    got = _map(_hits, jobs, a.jobs)
    print(f"\ntested at {a.per_hour:g} clips an hour: viewer clips found, of those no word was learned from")
    print(f"  {'channel':22} {'clips':>7} {'global':>7} {'+category':>10} {'+streamer':>10}")
    sums = []
    for title, rows in (("", got[: len(chans)]), ("our own streamers, any category:", got[len(chans) :])):
        if title and rows:
            print(f"  {title}")
        tot = [0, 0, 0, 0]
        for login, total, found in rows:
            print(f"  {login:22} {total:7d} {found[0]:7d} {found[1]:10d} {found[2]:10d}")
            tot = [tot[0] + total, *(t + f for t, f in zip(tot[1:], found, strict=True))]
        if rows:
            print(f"  {'all':22} {tot[0]:7d} {tot[1]:7d} {tot[2]:10d} {tot[3]:10d}")
        sums.append(tot)
    cat_sum, own_sum = sums
    return {
        "clips": cat_sum[0], "global": cat_sum[1], "category": cat_sum[2],
        "without_streamer": cat_sum[2] + own_sum[2], "streamer": cat_sum[3] + own_sum[3],
    }  # fmt: skip


def write_keywords(category: str, words: dict, streamers: dict, stats: list[dict]) -> None:
    """Merge the result into config/keywords.yaml (other categories and streamers in it are kept)."""
    import yaml

    path = config.CONFIG_DIR / "keywords.yaml"
    data = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    if words:
        cat = data.setdefault("categories", {}).setdefault(slug(category), {})
        cat.setdefault("match", [category.lower()])
        hours, moments = sum(s["hours"] for s in stats), sum(s["moments"] for s in stats)
        when = time.strftime("%Y-%m-%d")
        cat["learned"] = f"{when} from {len(stats)} channels, {hours:.0f} h of chat, {moments} moments"
        ranked = sorted(words.items(), key=lambda kv: -kv[1]["moments"])
        cat["keywords"] = {x: i["weight"] for x, i in ranked}
    for login, ws in streamers.items():
        entry = data.setdefault("streamers", {}).setdefault(login, {})
        entry["keywords"] = {x: i["weight"] for x, i in ws.items()}
    head = (
        "# Keywords learned from real chat by scripts/learn_keywords.py (D16). The detector adds a\n"
        "# category's words when a stream is in it and a streamer's words on that channel, on top of\n"
        "# detector.keywords in global.yaml. Edit freely: learn only replaces the category and the\n"
        "# streamers it was run for.\n"
    )
    path.write_text(head + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100))
    print(f"\nwrote {path}")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="download the chat of a category's recent broadcasts")
    c.add_argument("--category", help='as on Twitch, e.g. "Grand Theft Auto VI"')
    c.add_argument("--streamers", help="instead: these channels' recent broadcasts, whatever they streamed")
    c.add_argument("--channels", type=int, default=40, help="how many of the biggest live channels")
    c.add_argument("--also", help="more channel logins, comma-separated (your own streamers)")
    c.add_argument("--hours", type=float, default=10, help="hours of the category per channel, newest first")
    c.add_argument("--days", type=float, default=3, help="how far back to look for broadcasts")
    c.add_argument("--language", default=config.channel_language())
    c.add_argument(
        "--max-minutes", type=float, default=600, help="stop after this long; run again to continue"
    )
    c.add_argument("--shard", default="0/1", help="i/n: every n-th channel from the i-th (parallel runs)")
    c.set_defaults(fn=collect)
    ln = sub.add_parser("learn", help="find the words that mark a clipped moment, per category and streamer")
    ln.add_argument("--category", required=True)
    ln.add_argument("--validate", action="store_true", help="test the result on viewer clips held out")
    ln.add_argument("--write", action="store_true", help="save what passes that test to config/keywords.yaml")
    ln.add_argument("--force", action="store_true", help="write even what the test found no gain in")
    ln.add_argument("--min-gain", type=float, default=0.05, help="write: share more viewer clips found")
    ln.add_argument("--min-gain-clips", type=int, default=3, help="... and at least this many more")
    ln.add_argument("--jobs", type=int, default=6, help="worker processes")
    ln.add_argument("--streamers", help="whose words to write: logins, or all (default: streamers.yaml)")
    ln.add_argument("--min-english", type=float, default=0.03, help="share of plain English words in a chat")
    ln.add_argument("--min-moments-channel", type=int, default=10, help="moments a channel needs to count")
    ln.add_argument("--category-lift", type=float, default=2.0, help="times more common during a moment")
    ln.add_argument("--channel-lift", type=float, default=1.5, help="... in a single channel, to count it")
    ln.add_argument("--min-channels", type=int, default=5)
    ln.add_argument(
        "--channel-share", type=float, default=0.2, help="share of channels a category word needs"
    )
    ln.add_argument("--min-moments", type=int, default=15, help="moments in which the word was up")
    ln.add_argument("--moment-share", type=float, default=0.02, help="... and as a share of all moments")
    ln.add_argument("--max-one-moment", type=float, default=0.2, help="share of its use in a single moment")
    ln.add_argument("--streamer-moments-channel", type=int, default=15, help="moments a streamer needs")
    ln.add_argument("--streamer-lift", type=float, default=3.0)
    ln.add_argument(
        "--plain-lift", type=float, default=1.3, help="under this in the typical chat: plain talk"
    )
    ln.add_argument("--streamer-moments", type=int, default=5)
    ln.add_argument("--streamer-share", type=float, default=0.12, help="share of the streamer's moments")
    ln.add_argument("--streamer-one-moment", type=float, default=0.3)
    ln.add_argument("--streamer-max", type=int, default=20)
    ln.add_argument(
        "--detector-moments", type=float, default=0.0,
        help="also learn from what the detector clips today, this many an hour (0: viewer clips only)",
    )  # fmt: skip
    ln.add_argument("--max-weight", type=float, default=3.0, help="validate: no learned weight above this")
    ln.add_argument(
        "--ceiling", action="store_true", help="validate: learn from the test clips too (upper bound)"
    )
    ln.add_argument("--folds", type=int, default=4, help="validate: channel groups for the category words")
    ln.add_argument(
        "--per-hour", type=float, default=3.0, help="validate: clips an hour the detector may pick"
    )
    ln.set_defaults(fn=learn)
    return ap


def main() -> int:
    a = parser().parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
