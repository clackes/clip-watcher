from clipwatcher.judge.snap import heuristic_trim, reaction_s, snap_trim
from clipwatcher.models import Word


def W(s, e, t):
    return Word(start=s, end=e, text=t)


WORDS = [
    W(0.5, 0.9, "okay"),
    W(1.0, 1.4, "chat."),
    W(2.0, 2.3, "Watch"),
    W(2.35, 2.6, "this"),
    W(2.7, 3.2, "jump."),
    W(10.0, 10.4, "No"),
    W(10.5, 10.9, "way"),
    W(11.0, 11.6, "he"),
    W(11.7, 12.4, "made"),
    W(12.5, 13.0, "it!"),
    W(14.0, 14.5, "Next"),
    W(14.6, 15.0, "round"),
]


def test_start_never_cuts_a_word_and_never_rewinds_to_the_sentence_start():
    s, _ = snap_trim(2.45, 12.0, WORDS, 20)  # t0 lands inside "this"
    assert s == 2.3  # back to "this" (2.35 - 0.15 lead, floored at the end of "Watch"), not to "Watch"
    s, _ = snap_trim(5.0, 12.0, WORDS, 20)  # t0 in silence well before "No"
    assert s == 5.0


def test_heuristic_trim_starts_late():
    assert heuristic_trim(37.0, 59.0) == (27.0, 59.0)  # start 10 s before the reaction
    assert heuristic_trim(2.0, 59.0) == (0.0, 59.0)
    assert heuristic_trim(50.0, 59.0) == (29.0, 59.0)  # never shorter than render.clip_min_s (30 s)


def test_fit_length_keeps_clips_between_30_and_59_seconds():
    from clipwatcher.judge.snap import clip_bounds, fit_length

    assert clip_bounds() == (30.0, 59.0)
    assert fit_length(30.0, 44.0, 59.0) == (14.0, 44.0)  # too short: more setup, the ending stays
    assert fit_length(4.0, 20.0, 59.0) == (0.0, 30.0)  # no more setup to take: more aftermath
    assert fit_length(0.0, 75.0, 80.0) == (16.0, 75.0)  # too long: the oldest part goes
    assert fit_length(10.0, 45.0, 59.0) == (10.0, 45.0)
    assert fit_length(2.0, 9.0, 20.0) == (0.0, 20.0)  # a window shorter than 30 s is used whole
    assert reaction_s(12.5, 20.0) == 12.5 and reaction_s(None, 20.0) == 17.0


def test_end_finishes_the_sentence_and_keeps_a_tail():
    _, e = snap_trim(2.0, 11.2, WORDS, 20)  # t1 inside "he", sentence ends at "it!" 13.0
    assert e == 13.35


def test_end_in_a_pause_does_not_run_into_the_next_sentence():
    """Regression: the last word before the judge's end had no full stop (Whisper leaves it out often) and
    the end sat in the pause after it. The end was pushed on to the full stop of the NEXT sentence."""
    words = [
        W(10.0, 10.4, "No"), W(10.5, 10.9, "way"), W(11.0, 11.5, "bro"),  # ... 1.5 s pause ...
        W(13.0, 13.4, "Anyway"), W(13.5, 13.8, "next"), W(13.9, 14.2, "round."),
    ]  # fmt: skip
    assert snap_trim(2.0, 12.0, words, 20) == (2.0, 12.0)  # was (2.0, 14.55)
    assert snap_trim(2.0, 11.3, words, 20) == (2.0, 11.85)  # inside "bro": the word is finished, no more
    assert snap_trim(2.0, 11.6, words, 20) == (2.0, 11.85)  # right after it: only the tail
    # mid-line without punctuation: the line in progress is finished, up to its pause and not beyond
    run = [W(0.0, 0.3, "bro"), W(0.35, 0.6, "what"), W(0.65, 0.9, "the"), W(0.95, 1.4, "hell")]
    run += [W(2.4, 2.7, "ok"), W(2.75, 3.0, "so.")]
    assert snap_trim(0.0, 0.7, run, 10) == (0.0, 1.75)  # was (0.0, 3.35)


def test_a_pause_ends_a_sentence_exactly_where_the_judge_sees_a_new_line():
    from clipwatcher.judge import snap
    from clipwatcher.judge.compact import PHRASE_GAP_S, serialize_transcript_compact

    g = PHRASE_GAP_S
    words = [W(0.0, 0.4, "one"), W(0.4 + g + 0.05, 1.5, "two"), W(1.5 + g - 0.05, 2.5, "three")]
    assert [p[2] for p in serialize_transcript_compact(words)] == ["one", "two three"]
    assert snap._finished(words, 0) and not snap._finished(words, 1) and snap._finished(words, 2)


def test_end_tail_is_clamped_by_next_word_and_duration():
    _, e = snap_trim(2.0, 15.2, WORDS, 15.1)
    assert e == 15.1


def test_far_sentence_end_is_not_chased():
    words = [W(0.0, 0.4, "so"), W(0.5, 0.9, "basically"), W(1.0, 1.3, "we")] + [
        W(1.4 + i * 0.5, 1.8 + i * 0.5, "go") for i in range(20)
    ]
    _, e = snap_trim(0.0, 2.6, words, 30)  # t1 inside a word, no sentence end within 3 s
    assert 2.6 < e <= 2.8 + 0.35


def test_no_words_or_tiny_result_returns_input():
    assert snap_trim(1.0, 5.0, [], 10) == (1.0, 5.0)
    assert snap_trim(0.0, 0.5, [W(0.1, 0.3, "x.")], 10) == (0.0, 0.5)


def test_plan_stretches_a_short_judge_trim_to_the_shortest_clip_without_cutting_a_word(tmp_path):
    from clipwatcher.render import cold_open

    # a line every 4 s across a 59 s window; the judge kept only 30.0-44.0 (14 s)
    words = [W(t, t + 1.5, f"line{t}.") for t in range(0, 58, 4)]
    judged = {"source": "claude", "trim_start_s": 30.0, "trim_end_s": 44.0, "peak_rel_s": 41.0}
    pl = cold_open.plan(tmp_path / "x.mp4", 0.0, 59.0, 41.0, judged, words)
    assert 30.0 <= pl.t1 - pl.t0 <= 59.0 and pl.t1 >= 44.0  # more setup in front, the ending stays
    assert not any(w.start < pl.t0 < w.end for w in words)  # the new start is between words
    long = {"source": "claude", "trim_start_s": 0.0, "trim_end_s": 75.0, "peak_rel_s": 70.0}
    pl = cold_open.plan(tmp_path / "x.mp4", 0.0, 80.0, 70.0, long, [])
    assert pl.t1 - pl.t0 <= 59.0 and pl.t1 == 75.0  # too long: the oldest part goes
    ok = {"source": "claude", "trim_start_s": 10.0, "trim_end_s": 45.0, "peak_rel_s": 40.0}
    pl = cold_open.plan(tmp_path / "x.mp4", 0.0, 59.0, 41.0, ok, words)
    assert (pl.t0, pl.t1) == (10.0, 45.0)  # inside the bounds: the judge's trim is used as it is
