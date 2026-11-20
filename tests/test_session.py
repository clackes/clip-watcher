"""D13: the stream as a table, whole-stream baseline, stream-relative gate (detect/session.py, live.py)."""

import random

from clipwatcher import config
from clipwatcher.detect.detector import _FLOORS, _kw_patterns, confirm, detect, loud_z
from clipwatcher.detect.live import LiveDetector
from clipwatcher.detect.session import SessionTable
from clipwatcher.models import Candidate, ChatMessage

BASE = 1_790_000_000
_WORDS = "the a so that was this is what how did he she they just really when why who then now look".split()
_REACT = ["KEKW", "LMAO", "no way", "OMEGALUL"]


def _talk(rng):
    return " ".join(rng.choice(_WORDS) for _ in range(rng.randint(4, 8)))


def steady(rng, t0, t1, rate=1.0, users=60):
    """Background chat: `rate` messages a second, every message different."""
    out, t = [], float(t0)
    while t < t1:
        t += rng.expovariate(rate)
        out.append(ChatMessage(int((BASE + t) * 1000), f"u{rng.randrange(users)}", _talk(rng)))
    return out


def burst(rng, at, n, texts, span=8.0):
    """n different chatters within `span` s; texts = list to pick from, or a function for free text."""
    pick = texts if callable(texts) else (lambda r: r.choice(texts))
    return [
        ChatMessage(int((BASE + at + rng.uniform(0, span)) * 1000), f"fan{i}", pick(rng)) for i in range(n)
    ]


def run(msgs, prime_until=None):
    msgs = sorted(msgs, key=lambda m: m.ts_ms)
    det = LiveDetector("nobody")
    if prime_until is not None:
        det.prime([m for m in msgs if m.ts_ms / 1000 < BASE + prime_until])
        msgs = [m for m in msgs if m.ts_ms / 1000 >= BASE + prime_until]
    return det, [c for m in msgs if (c := det.feed(m))] + det.drain()


def near(cands, at, tol=12):
    return [c for c in cands if abs(c.signals["fire_s"] - (BASE + at)) <= tol]


# ---------------------------------------------------------------------------------------- the table
def test_table_level_is_half_stream_half_last_hour():
    tab = SessionTable(_FLOORS, {"U": 1.0}, level_s=100, session_share=0.5)
    for _ in range(300):
        tab.push(10, 10, 5, 0.5)
    for _ in range(100):  # chat doubles: the last "hour" is at 20, the stream median still at 10
        tab.push(20, 20, 10, 0.5)
    tab.refresh()
    assert tab.med["U"] == 10 and tab.recent()[0] == 20
    score, zs = tab.score((30, 30, 15, 0.5))
    assert zs["U"] == (30 - 15) / tab.sd["U"] and score == zs["U"]  # level 15 = half of each
    whole = SessionTable(_FLOORS, {"U": 1.0}, level_s=0)
    for u in [10] * 300 + [20] * 100:
        whole.push(u, u, 5, 0.5)
    assert whole.score((30, 30, 15, 0.5))[1]["U"] == (30 - 10) / whole.sd["U"]


def test_table_gate_quantiles_cover_every_scorable_second():
    rng = random.Random(3)
    tab = SessionTable(_FLOORS, {"U": 0.5, "M": 0.5})
    for i in range(2000):
        u = rng.randint(8, 12) + (40 if i % 400 == 399 else 0)  # five spikes
        tab.push(u, u, 5, 0.5, scorable=i >= 60)
    tab.refresh()
    assert tab.scored == 1940
    spike = tab.score((50, 50, 5, 0.5))[0]
    assert tab.quantile(0.99) < spike and tab.share_below(spike) > 0.995
    assert tab.share_below(tab.score((10, 10, 5, 0.5))[0]) < 0.8


def test_table_marathon_keeps_the_last_rows():
    tab = SessionTable(_FLOORS, {"U": 1.0}, max_rows=1000)
    for i in range(3000):
        tab.push(i % 7, i % 7, 1, 0.5, sec=i)
    assert 1000 <= len(tab) <= 1600 and tab.secs[-1] == 2999 and len(tab.cols["U"]) == len(tab)


# ------------------------------------------------------------------------------------- the detector
def test_held_keys_count_as_the_keyword():
    pats = _kw_patterns(config.keywords_for(None))
    hit = lambda text: {k for k, p, _ in pats if p.search(text)}  # noqa: E731
    assert hit("LMAOOOO") == {"LMAO"} and hit("WWWW") == {"W"} and hit("KEKWW") == {"KEKW"}
    assert hit("LETS GOOOO") == {"LETS GO"} and hit("noooo way") == {"no way"}
    assert (
        hit("kkkkkk") == {"kkkk"} and not hit("kkk") and not hit("k")
    )  # repeats in the keyword are a minimum
    assert hit("??????") == {"?????"} and not hit("????")
    assert hit("looool") == {"lol"} and hit("broo") == {"bro"} and hit("nahhh") == {"nah"}
    god = _kw_patterns({"god": 1.0, "bot": 1.0})  # a letter typed twice inside a word is another word
    assert [k for k, p, _ in god if p.search("oh my goood")] == ["god"]
    assert not [k for k, p, _ in god if p.search("good boot")]


def test_a_burst_of_plain_messages_is_a_spike_without_any_keyword(monkeypatch):
    """The M column on its own. With the owner's reaction gate (detector.min_reaction_share, 18 Nov) such a
    burst is not clipped: that is checked last."""
    import copy

    rng = random.Random(1)
    msgs = steady(rng, 0, 1800) + burst(rng, 1500, 60, _talk)
    gated = copy.deepcopy(config.global_config())
    open_cfg = copy.deepcopy(gated)
    open_cfg["detector"]["min_reaction_share"] = 0.0
    monkeypatch.setattr(config, "global_config", lambda: open_cfg)
    _, live = run(msgs)
    assert len(near(live, 1500)) == 1 and near(live, 1500)[0].signals["zM"] > 5
    batch = detect(sorted(msgs, key=lambda m: m.ts_ms), streamer="nobody", top=1)
    assert abs(batch[0].peak_s - (BASE + 1492)) <= 10  # epoch stamps in, epoch stamps out
    monkeypatch.setattr(config, "global_config", lambda: gated)
    assert not near(run(msgs)[1], 1500)  # no real reaction in it: not a moment


def test_unison_reaction_with_held_keys_is_not_muted_as_copy_paste():
    rng = random.Random(2)
    _, out = run(steady(rng, 0, 900) + burst(rng, 700, 60, ["WWWW", "WWWWWW", "W", "WW"]))
    assert near(out, 700)
    _, out = run(steady(rng, 0, 900) + burst(rng, 700, 60, ["IWASHERE", "IWASHERE", "yo"]))
    assert not out


def test_spike_right_after_a_long_lively_stretch_is_still_clipped():
    """The trailing baseline made the lively stretch its own normal: a spike after 10 min of 3x chat did not
    fire for 5+ minutes. The scale now comes from the whole stream."""
    rng = random.Random(4)
    msgs = steady(rng, 0, 3300) + steady(rng, 2200, 2800, rate=2.0, users=120) + burst(rng, 3000, 50, _REACT)
    _, out = run(msgs)
    assert near(out, 3000) and near(out, 3000)[0].chat_score > 10


def test_small_spike_does_not_shadow_the_real_one_behind_it():
    """The old 45 s cooldown started at any fire: a small burst 30 s before the real spike took the clip (with
    a window that ended before the moment) or, when it was too weak to keep, left no clip at all."""
    rng = random.Random(5)
    real = burst(rng, 700, 70, _REACT)
    kept_small = steady(rng, 0, 900) + burst(rng, 670, 10, ["lol", "KEKW"], span=4) + real
    _, out = run(kept_small)
    big = [c for c in out if c.chat_score > 15]
    assert len(big) == 1 and abs(big[0].peak_s - (BASE + 692)) <= 10  # anchored at the real spike
    too_weak = steady(rng, 0, 900) + burst(rng, 672, 5, ["hm ok", "wait what"], span=3) + real
    _, out = run(too_weak)
    assert len(out) == 1 and out[0].chat_score > 15 and abs(out[0].peak_s - (BASE + 692)) <= 10


def test_fire_in_a_long_run_is_anchored_to_its_own_burst():
    """After chat triples for good, the score stays above the pre-threshold for minutes; a spike 8 min in
    must not be reported at the moment the run began."""
    rng = random.Random(6)
    msgs = steady(rng, 0, 2000) + steady(rng, 1200, 2000, rate=2.0, users=150) + burst(rng, 1700, 80, _REACT)
    _, out = run(msgs)
    spike = near(out, 1700, tol=15)
    assert spike and abs(spike[0].peak_s - (BASE + 1692)) <= 15


def test_only_a_gift_bomb_mutes():
    rng = random.Random(7)

    def with_gift(n):
        gift = ChatMessage((BASE + 695) * 1000, "gifter", "", is_notice=True, notice_type="submysterygift")
        gift.viewers = n
        return run(steady(rng, 0, 900) + [gift] + burst(rng, 700, 60, _REACT))[1]

    assert near(with_gift(1), 700) and not with_gift(20)


def test_primed_detector_knows_the_stream_before_it_joined():
    rng = random.Random(8)
    hist = steady(rng, 0, 7200) + burst(rng, 6000, 90, _REACT)
    for t in (1000, 2500, 4000, 5500):
        hist += burst(rng, t, 50, _REACT)
    live = steady(rng, 7200, 7600) + burst(rng, 7290, 60, _REACT)
    det, out = run(hist + live, prime_until=7200)
    assert len(det.spikes) >= 6 and len(det.table) > 7000  # 5 from history, kept for ranking, not emitted
    assert [round(c.signals["fire_s"] - BASE, -1) for c in out] == [7290]  # 90 s after joining
    c = out[0]
    assert c.signals["gate"] > 5.0  # this stream's own bar, not the fixed 5.0 of a cold start
    assert c.signals["session_rank"] == 2 and c.signals["session_pct"] > 0.99  # behind the 90-chatter one
    assert c.end_s - c.start_s == 59  # config: before_s 40 + after_s 19


def test_batch_and_live_agree_on_a_long_chat():
    rng = random.Random(9)
    msgs = steady(rng, 0, 5400)
    spikes = [900, 1700, 2600, 3300, 4100, 5000]
    for i, t in enumerate(spikes):
        msgs += burst(rng, t, 40 + 10 * i, _REACT)
    msgs.sort(key=lambda m: m.ts_ms)
    batch = detect(msgs, streamer="nobody", top=6, gap_s=60)
    _, live = run(msgs)
    for t in spikes:
        assert any(abs(c.peak_s - (BASE + t - 8)) <= 12 for c in batch), t
        assert near(live, t), t
    assert batch[0].signals["session_rank"] == 1 and abs(batch[0].peak_s - (BASE + 4992)) <= 12


# ------------------------------------------------------------------------ confirmation (D3)
def test_loud_z_measures_the_scream_at_the_moment():
    talk = [(i / 10, -30.0 + (i % 5) * 0.5) for i in range(590)]  # 59 s of talking around -29 dB
    scream = [(t, -8.0 if 40.0 <= t < 41.0 else v) for t, v in talk]
    assert loud_z(scream, 38.0) > 10 and loud_z(scream, 5.0) < 2  # at the moment, not anywhere in the clip
    assert abs(loud_z(talk, 38.0)) < 2 and loud_z([], 38.0) is None
    silent = [(i / 10, -120.0) for i in range(590)]
    assert loud_z(silent, 38.0) is None  # digital silence is no audio, not a quiet level


def test_viewer_clip_rescues_a_spike_that_fired_under_the_keep_gate():
    rng = random.Random(11)
    # six more chatters, half of them laughing: over the fire threshold (3.0), under the keep threshold (5.0)
    det, out = run(steady(rng, 0, 900) + burst(rng, 700, 6, ["lol", "nice one man that was good"], span=6))
    assert out == [] and len(det.near) == 1
    near = det.near[0]
    assert det.rescue(near.peak_s + 400) == []  # a clip of some other moment
    saved = det.rescue(near.peak_s + 12)  # a viewer clipped this one
    assert saved == [near] and saved[0].signals["viewer_clip"] == 1.0 and not det.near  # handed out once
    assert confirm(saved[0], [near.peak_s + 12]).final_score == near.chat_score + 1.5


def test_viewer_clips_confirm_a_moment_only_when_they_stand_out():
    """One clip confirms a moment where clips are rare. On a 37k-viewer Kick stream viewers made four clips a
    minute, so some clip covered every moment and "a viewer clipped it" said nothing."""
    from clipwatcher.detect.detector import clip_confirmed

    assert clip_confirmed([(965.0, 1000.0)], 990.0) and not clip_confirmed([(965.0, 1000.0)], 1100.0)
    assert not clip_confirmed([], 990.0)
    # a busy channel: a 60 s clip saved every 15 s for half an hour -> four clips cover any instant
    busy = [(t - 60.0, float(t)) for t in range(0, 1800, 15)]
    assert not clip_confirmed(busy, 1700.0)
    burst = busy + [(1640.0 + i, 1705.0 + i) for i in range(8)]  # eight more on one moment
    assert clip_confirmed(burst, 1700.0) and not clip_confirmed(burst, 1000.0)
    c = Candidate("s", 990.0, 950.0, 1009.0, 4.0, 4.0)
    assert confirm(c, [1000.0]).final_score == 5.5 and confirm(c, [2000.0]).final_score == 4.0
