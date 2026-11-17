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
    assert heuristic_trim(17.0, 30.0) == (7.0, 30.0)  # start 10 s before the reaction
    assert heuristic_trim(2.0, 30.0) == (0.0, 30.0)
    assert heuristic_trim(26.0, 30.0) == (16.0, 30.0)  # 14 s left, >= 12 s kept
    assert reaction_s(12.5, 20.0) == 12.5 and reaction_s(None, 20.0) == 17.0


def test_end_finishes_the_sentence_and_keeps_a_tail():
    _, e = snap_trim(2.0, 11.2, WORDS, 20)  # t1 inside "he", sentence ends at "it!" 13.0
    assert e == 13.35


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
